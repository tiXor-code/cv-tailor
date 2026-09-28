"""Adopting a job he opened himself (Scout Fill 2026-09-28): find the JD
(JSON-LD on the page or its parents, else his page's text), add it to his list
once, tailor it. Fetching is guarded: https + public addresses only."""
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from cv_tailor import adopt  # noqa: E402

DESC = "<p>We build fixture agents for care teams.</p>" + "<p>Build LLM workflows and evals.</p>" * 20
LD = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Applied AI Engineer",
      "hiringOrganization": {"@type": "Organization", "name": "Fixture Health"},
      "jobLocationType": "TELECOMMUTE", "applicantLocationRequirements": {"@type": "Country", "name": "Exampleland"},
      "url": "https://jobs.example.com/companies/fixture/99-applied-ai-engineer",
      "description": DESC.replace("<", "&lt;").replace(">", "&gt;")}
POSTING_HTML = f'<html><script type="application/ld+json">{json.dumps(LD)}</script></html>'


def test_candidates_walk_up_from_the_form_to_the_posting():
    assert adopt.candidate_urls("https://jobs.example.com/companies/fixture/99/apply/cv?track=1") == [
        "https://jobs.example.com/companies/fixture/99/apply/cv",
        "https://jobs.example.com/companies/fixture/99/apply",
        "https://jobs.example.com/companies/fixture/99"]


def test_posting_found_on_a_parent_page_with_escaped_html():
    seen = []

    def fetcher(u):
        seen.append(u)
        if u.endswith("/99"):
            return "https://jobs.example.com/companies/fixture/99-applied-ai-engineer", POSTING_HTML
        return u, "<html>log in to apply</html>"

    p = adopt.find_posting("https://jobs.example.com/companies/fixture/99/apply/cv", fetcher=fetcher)
    assert p["title"] == "Applied AI Engineer" and p["company"] == "Fixture Health"
    assert p["location"] == "Remote - Exampleland"
    assert "fixture agents for care teams" in p["description"] and "<p>" not in p["description"]
    assert len(seen) == 3


def test_page_text_fallback_and_nothing_found():
    def nothing(u):
        return u, "<html></html>"
    text = "Build fixture agents. " * 60
    p = adopt.find_posting("https://careers.fixtureco.example/jobs/7", "AI Engineer | Fixture Co", text, fetcher=nothing)
    assert p["title"] == "AI Engineer" and p["company"] == "fixtureco"
    with pytest.raises(adopt.AdoptError):
        adopt.find_posting("https://careers.fixtureco.example/jobs/7", "x", "too short", fetcher=nothing)


def test_linkedin_is_never_fetched_only_his_page_text_is_used():
    def boom(u):
        raise AssertionError("fetched LinkedIn")
    text = "Build fixture agents. " * 60
    p = adopt.find_posting("https://www.linkedin.com/jobs/view/123456789/", "AI Engineer", text,
                           "Fixture Co", fetcher=boom)
    assert p["company"] == "Fixture Co"


@pytest.mark.parametrize("url", ["http://example.com/job", "https://127.0.0.1/job", "https://localhost/job",
                                 "https://169.254.169.254/latest", "https://10.0.0.5/x", "https://evil.com\\@x/"])
def test_unsafe_urls_are_refused(url):
    with pytest.raises(adopt.AdoptError):
        adopt.fetch(url)


@pytest.fixture
def script(monkeypatch, tmp_path):
    import importlib.util
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("adopt_job", ROOT / "scripts" / "adopt_job.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run(script, monkeypatch, payload, **kw):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = script.main(**kw)
    return rc, (json.loads(out.getvalue()) if out.getvalue() else None)


def test_adopts_once_onto_his_list_tailored(script, monkeypatch, tmp_path):
    calls = []

    def assembler(entry, day):
        calls.append(entry["id"])
        pkg = tmp_path / "pkg"
        pkg.mkdir(exist_ok=True)
        return {"package_dir": str(pkg), "cv_path": str(pkg / "cv.pdf"), "cover_letter_path": str(pkg / "cl.md")}

    def fetcher(u):
        return "https://jobs.example.com/companies/fixture/99-applied-ai-engineer", POSTING_HTML

    payload = {"url": "https://jobs.example.com/companies/fixture/99/apply/cv?t=1"}
    rc, out = _run(script, monkeypatch, payload, fetcher=fetcher, assembler=assembler)
    assert rc == 0 and out["reused"] is False and out["company"] == "Fixture Health"
    day = json.loads((tmp_path / out["date"] / "jobs.json").read_text())
    e = next(x for x in day if x["id"] == out["id"])
    assert e["status"] == "handed_off" and e["source"] == "extension"
    assert e["apply_target"] == "https://jobs.example.com/companies/fixture/99/apply/cv?t=1"
    assert e["cv_path"].endswith("cv.pdf")
    desc = json.loads((tmp_path / out["date"] / "descriptions.json").read_text())[out["id"]]
    assert "fixture agents" in desc
    rc, again = _run(script, monkeypatch, payload, fetcher=fetcher, assembler=assembler)
    assert rc == 0 and again["reused"] is True and again["id"] == out["id"]
    assert calls == [out["id"]]


def test_bad_input_and_no_jd_exit_codes(script, monkeypatch):
    assert _run(script, monkeypatch, {"url": "http://x.example/job"})[0] == 2
    assert _run(script, monkeypatch, {"nope": 1})[0] == 2
    rc, _ = _run(script, monkeypatch, {"url": "https://jobs.example.com/a/b"},
                 fetcher=lambda u: (u, "<html></html>"))
    assert rc == 3
