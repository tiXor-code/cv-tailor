"""The ATS + JD-coverage pass wired into assemble_package (cv_tailor.ats_pass).

tailor/cover_letter are faked; render_html runs for real against the repo
templates. Most tests fake render_pdf (pdftotext then can't read it, so JD
coverage falls back to cv.html); one renders a real PDF end to end. Profile and
JD are fictional (tests/fixtures/ats/)."""
import json
import shutil
from pathlib import Path

import pytest

from cv_tailor import assemble as assemble_mod
from cv_tailor.assemble import assemble_package

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "ats"
JD = (FIX / "jd_sample.txt").read_text()

FIRST = {
    "job_meta": {"company": "x", "role": "x", "location": "Remote", "jd_url": None,
                 "seniority_signal": "mid"},
    "chosen_summary_id": "default",
    "summary_rewrite": "Automation engineer who builds internal tools.",
    "experience_ids_ordered": ["studio"],
    "experience_bullets": {"studio": [0]},
    "project_ids": [],
    "skills_emphasis": ["Python"],
    "jd_keywords_matched": [],
    "gaps_honest": ["Kubernetes"],
    "one_line_pitch": "Fit.",
}


class FakeTailor:
    """First call -> FIRST. Hinted call -> `second` (a dict, or a callable)."""

    def __init__(self, second=None):
        self.calls = []
        self.second = second

    def __call__(self, profile, jd_text, *, client=None, deployment=None):
        self.calls.append(jd_text)
        if "Tailoring hint" in jd_text and self.second is not None:
            return json.loads(json.dumps(self.second))
        return json.loads(json.dumps(FIRST))


def _fake_render_pdf(html, css_path, out_path):
    Path(out_path).write_bytes(b"%PDF-1.4 fake\n")
    return Path(out_path)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(assemble_mod, "cover_letter",
                        lambda *a, **k: " ".join(["shipped"] * 140))
    monkeypatch.setattr(assemble_mod, "render_pdf", _fake_render_pdf)
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(FIX / "profile_ats.yaml"))
    monkeypatch.setenv("CV_TAILOR_TEMPLATES", str(ROOT / "templates"))


def _run(tmp_path, monkeypatch, tailor, jd=JD):
    monkeypatch.setattr(assemble_mod, "tailor", tailor)
    day = tmp_path / "2026-01-15"
    day.mkdir(parents=True)
    (day / "descriptions.json").write_text(json.dumps({"job1": jd}))
    entry = {"id": "job1", "title": "AI Automation Engineer", "company": "Acme Robotics",
             "url": "https://example.com/jobs/1", "source": "lever"}
    result = assemble_package(entry, "2026-01-15", queue_dir=tmp_path, client=object())
    meta = json.loads((Path(result["package_dir"]) / "meta.json").read_text())
    return result, meta


def test_second_pass_hints_only_supported_terms_and_is_adopted(tmp_path, monkeypatch):
    second = dict(FIRST, experience_bullets={"studio": [0, 1]})
    tailor = FakeTailor(second)
    result, meta = _run(tmp_path, monkeypatch, tailor)

    assert len(tailor.calls) == 2
    hint = tailor.calls[1].split("Tailoring hint")[1]
    assert "RAG" in hint and "LLM" in hint
    assert "Kubernetes" not in hint and "Terraform" not in hint

    refine = meta["ats_refine"]
    assert refine["attempted"] and refine["adopted"]
    assert meta["jd_match_pct"] > refine["first_pass_match_pct"]
    assert "RAG" not in meta["jd_missing_supported"]
    assert "kubernetes" in [t.lower() for t in meta["jd_missing_unsupported"]]
    html = (Path(result["package_dir"]) / "cv.html").read_text()
    assert "retrieval-augmented generation" in html
    for key in ("ats", "jd_match_pct", "jd_missing_supported", "jd_missing_unsupported"):
        assert key in meta
    assert {"score", "files", "cross_file"} <= set(meta["ats"])


def test_second_pass_that_breaks_honesty_is_discarded(tmp_path, monkeypatch):
    invented = dict(FIRST, experience_ids_ordered=["studio", "made_up_job"])
    result, meta = _run(tmp_path, monkeypatch, FakeTailor(invented))

    refine = meta["ats_refine"]
    assert refine["attempted"] and not refine["adopted"]
    assert "honesty guard" in refine["reason"]
    assert "RAG" in meta["jd_missing_supported"]
    assert "retrieval-augmented generation" not in (Path(result["package_dir"]) / "cv.html").read_text()


def test_second_pass_that_lowers_coverage_is_rolled_back(tmp_path, monkeypatch):
    worse = dict(FIRST, summary_rewrite="Short.", skills_emphasis=[],
                 experience_ids_ordered=["acme"], experience_bullets={"acme": [0]})
    result, meta = _run(tmp_path, monkeypatch, FakeTailor(worse))
    assert not meta["ats_refine"]["adopted"]
    html = (Path(result["package_dir"]) / "cv.html").read_text()
    assert "Example Studio" in html  # first-pass CV restored on disk


def test_no_supported_gaps_means_single_tailor_call(tmp_path, monkeypatch):
    tailor = FakeTailor(dict(FIRST))
    jd = "Kubernetes and Terraform. Kubernetes operators. Terraform modules."
    _, meta = _run(tmp_path, monkeypatch, tailor, jd=jd)
    assert len(tailor.calls) == 1
    assert meta["ats_refine"]["attempted"] is False
    assert meta["jd_missing_supported"] == []


def test_second_pass_llm_error_keeps_first_pass(tmp_path, monkeypatch):
    class Boom(FakeTailor):
        def __call__(self, profile, jd_text, *, client=None, deployment=None):
            if "Tailoring hint" in jd_text:
                self.calls.append(jd_text)
                raise RuntimeError("rate limited")
            return super().__call__(profile, jd_text, client=client)

    _, meta = _run(tmp_path, monkeypatch, Boom())
    assert "rate limited" in meta["ats_refine"]["reason"]
    assert not meta["ats_refine"]["adopted"]


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext missing")
def test_real_pdf_is_simulated(tmp_path, monkeypatch):
    pytest.importorskip("weasyprint")
    from cv_tailor.render import render_pdf
    monkeypatch.setattr(assemble_mod, "render_pdf", render_pdf)
    _, meta = _run(tmp_path, monkeypatch, FakeTailor(dict(FIRST, experience_bullets={"studio": [0, 1]})))
    pdf = meta["ats"]["files"][0]
    assert pdf["path"] == "cv.pdf"
    statuses = {c["name"]: c["status"] for c in pdf["checks"]}
    assert statuses["text layer"] == "PASS"
    assert statuses["layout: reading order"] == "PASS"
    assert statuses["contact: email"] == "PASS"
