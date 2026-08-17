"""scripts/weekly_scan.py -- the dormant weekly digest scanner.

Its launchd plist is `.disabled`, so nothing runs it on a schedule today, but
it shares sources.yaml and the SerpAPI key with the daily scan: an accidental
run has real cost and real exposure, and Phase 3 keeps adding sources to the
file it reads. Loaded as a standalone script module (it lives in scripts/, not
the package). Azure/Sheets/Telegram are monkeypatched out -- nothing here
reaches the network, and ROOT is redirected at tmp_path so no digest is ever
written into the repo's scans/ directory.
"""
import importlib.util
from pathlib import Path

import pytest

from cv_tailor.job_sources import JobPosting

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "weekly_scan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("weekly_scan_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _job(org="Acme", title="AI Engineer"):
    return JobPosting(source="greenhouse", org=org, title=title,
                      location="Remote - EU", url="https://acme.example/1",
                      description="Python", raw_id="1")


def _run(mod, monkeypatch, tmp_path, *, jobs=None, score_job=None, argv=None):
    """Drive main() with every external edge stubbed. Returns nothing; the
    caller asserts on captured stderr or on what its own stubs recorded."""
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  - kind: serpapi\n    query: \"ai engineer remote europe\"\n")
    monkeypatch.setattr(mod, "load_profile", lambda *a, **kw: {"target_keywords": ["ai"]})
    monkeypatch.setattr(mod, "build_azure_client", lambda *a, **kw: object())
    monkeypatch.setattr(mod, "fetch_all", lambda sources, **kw: list(jobs or []))
    monkeypatch.setattr(mod, "score_job", score_job or (lambda *a, **kw: {"score": 0}))
    monkeypatch.setattr(mod, "format_digest", lambda *a, **kw: "# digest\n")
    monkeypatch.setattr(mod, "format_digest_for_telegram", lambda *a, **kw: "digest")
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: False)
    monkeypatch.setattr(mod, "get_pipeline_worksheet",
                        lambda *a, **kw: pytest.fail("must not reach Sheets"))
    mod.main(argv or ["--no-dedupe"])


def test_score_failure_line_cannot_be_forged_by_a_posting(mod, monkeypatch, tmp_path, capsys):
    """org, title AND the exception text are all attacker-influenced: anyone can
    post a job, and score_job is fed the posting's own description, so an
    HTTP/JSON error can echo it back verbatim. One failure must stay one line."""
    hostile = _job(org="Acme\n  got 999 total postings",
                   title="AI Engineer\nscoring 0 jobs via Azure OpenAI")

    def boom(*a, **kw):
        raise RuntimeError("upstream said:\n  digest written: /dev/null\nand more")

    _run(mod, monkeypatch, tmp_path, jobs=[hostile], score_job=boom)

    err = capsys.readouterr().err
    failure_lines = [ln for ln in err.splitlines() if "score failed" in ln]
    assert len(failure_lines) == 1
    line = failure_lines[0]
    assert "\r" not in line
    # the injected text survives as content on the one line, flattened
    assert "got 999 total postings" in line and "digest written" in line
    # ...and none of the three forged status lines it carried became a line of
    # its own (each one mimics a real line this script prints, which is the
    # point: a human greps this log)
    injected = [ln for ln in err.splitlines()
                if ln.strip().startswith(("got 999", "scoring 0", "digest written: /dev/null"))]
    assert injected == [], f"a posting forged its own log line: {injected}"


def test_fetch_all_is_called_with_a_real_serp_budget(mod, monkeypatch, tmp_path):
    """weekly_scan pulls the same sources.yaml as the daily scan, including its
    5 serpapi queries. Called without serp_budget=, every one of those fires
    UNBUDGETED against a key capped at 90/mo out of a 250/mo pool shared with
    another project -- budget.SerpBudget exists precisely to stop that."""
    from cv_tailor.budget import SerpBudget

    budget_path = tmp_path / "data" / "serpapi_budget.json"
    monkeypatch.setattr(mod, "BUDGET_PATH", budget_path)
    seen = {}

    def spy_fetch_all(sources, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(mod, "ROOT", tmp_path)
    (tmp_path / "sources.yaml").write_text(
        "sources:\n  - kind: serpapi\n    query: \"ai engineer remote europe\"\n")
    monkeypatch.setattr(mod, "load_profile", lambda *a, **kw: {"target_keywords": ["ai"]})
    monkeypatch.setattr(mod, "build_azure_client", lambda *a, **kw: object())
    monkeypatch.setattr(mod, "fetch_all", spy_fetch_all)
    monkeypatch.setattr(mod, "format_digest", lambda *a, **kw: "# digest\n")
    monkeypatch.setattr(mod, "format_digest_for_telegram", lambda *a, **kw: "digest")
    monkeypatch.setattr(mod, "send_text", lambda *a, **kw: False)

    mod.main(["--no-dedupe"])

    budget = seen.get("serp_budget")
    assert isinstance(budget, SerpBudget), f"fetch_all got {seen!r}"
    assert budget.path == budget_path
    assert budget.monthly_cap == 90


def test_budget_file_is_the_one_the_daily_scan_counts_against(mod):
    """A separate counter file would let the two scanners spend 90 each. Static
    assertion -- no run, no file touched."""
    assert mod.BUDGET_PATH == ROOT / "data" / "serpapi_budget.json"
