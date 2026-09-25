"""scripts/answer_questions.py: Scout's answer engine for the one-click fill
extension (Teodor, 2026-09-25). The extension sends the questions it found on
a live form; this returns Scout's answers, or null where only he can answer.
Input comes from a web page, so it is untrusted and size-capped."""
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(ROOT / "tests" / "fixtures" / "profile_minimal.yaml"))
    spec = importlib.util.spec_from_file_location("answer_questions", ROOT / "scripts" / "answer_questions.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["answer_questions"] = m
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "load_answers", lambda *a, **k: {
        "salary_fulltime_gross_eur_month": 1234,
        "question_answers": [{"match": "Have you shipped a fixture", "answer": "Yes, the fixture."}]})
    return m


def _queue(tmp_path, **extra):
    d = tmp_path / "2026-09-25"
    (d / "pkg").mkdir(parents=True)
    (d / "pkg" / "cover_letter.md").write_text("I build fixture agents.")
    e = {"id": "job-1", "status": "handed_off", "company": "Fixture Co", "title": "AI Engineer",
         "package_dir": str(d / "pkg"), "cover_letter_path": str(d / "pkg" / "cover_letter.md"), **extra}
    (d / "jobs.json").write_text(json.dumps([e]))


def _run(mod, payload, monkeypatch, client=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = mod.main(client=client)
    return rc, (json.loads(out.getvalue()) if out.getvalue() else None)


def test_contact_saved_answers_and_needs_you(mod, monkeypatch, tmp_path):
    _queue(tmp_path)
    rc, out = _run(mod, {"date": "2026-09-25", "id": "job-1", "questions": [
        {"label": "Have you shipped a fixture to production?", "kind": "textarea", "required": True},
        {"label": "Describe your favourite colour", "kind": "text", "required": True},
    ]}, monkeypatch)
    assert rc == 0
    assert out["contact"]["email"] == "test@example.com"
    assert out["contact"]["first_name"] == "Test" and out["contact"]["last_name"] == "User"
    assert out["cover_letter"] == "I build fixture agents."
    a = {x["label"]: x for x in out["answers"]}
    assert a["Have you shipped a fixture to production?"]["value"] == "Yes, the fixture."
    assert a["Describe your favourite colour"]["value"] is None
    assert a["Describe your favourite colour"]["needs_you"] is True


def test_application_only_consent_is_ticked_marketing_is_not(mod, monkeypatch, tmp_path):
    _queue(tmp_path)
    _, out = _run(mod, {"date": "2026-09-25", "id": "job-1", "questions": [
        {"label": "I consent to Fixture Co processing my data for this application", "kind": "consent", "required": True},
        {"label": "Send me marketing updates and newsletters", "kind": "consent", "required": False},
    ]}, monkeypatch)
    a = [x["value"] for x in out["answers"]]
    assert a == ["yes", None]


def test_unknown_job_exits_4(mod, monkeypatch, tmp_path):
    _queue(tmp_path)
    rc, _ = _run(mod, {"date": "2026-09-25", "id": "nope", "questions": []}, monkeypatch)
    assert rc == 4


def test_untrusted_input_is_capped_and_validated(mod, monkeypatch, tmp_path):
    _queue(tmp_path)
    rc, _ = _run(mod, {"date": "../etc", "id": "job-1", "questions": []}, monkeypatch)
    assert rc == 2
    rc, out = _run(mod, {"date": "2026-09-25", "id": "job-1", "questions": [
        {"label": "x" * 6000, "kind": "text", "required": False}] * 200}, monkeypatch)
    assert rc == 0 and len(out["answers"]) == mod.MAX_QUESTIONS
    assert all(len(x["label"]) <= mod.MAX_LABEL for x in out["answers"])
    rc, _ = _run(mod, {"date": "2026-09-25", "id": "job-1", "questions": [
        {"label": "Q", "kind": "<script>", "required": True}]}, monkeypatch)
    assert rc == 2


def test_save_answers_script_stores_and_rejects_bad_input(monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("save_answers", ROOT / "scripts" / "save_answers.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    target = tmp_path / "answers.yaml"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"answers": [{"label": "Q?", "value": "A."}]})))
    out = io.StringIO(); monkeypatch.setattr(sys, "stdout", out)
    assert m.main(path=target) == 0 and json.loads(out.getvalue()) == {"saved": 1}
    assert "A." in (tmp_path / "answers_saved.yaml").read_text()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"answers": "nope"})))
    assert m.main(path=target) == 2


def test_a_job_not_on_his_list_is_answered_from_profile_and_answers(mod, monkeypatch, tmp_path):
    """LinkedIn Easy Apply on any job (Teodor, 2026-09-25): no queue entry,
    so no cover letter -- profile, answers.yaml and his saved answers only."""
    rc, out = _run(mod, {"questions": [
        {"label": "Have you shipped a fixture to production?", "kind": "textarea", "required": True},
        {"label": "Email address", "kind": "text", "required": True},
    ]}, monkeypatch)
    assert rc == 0
    assert [a["value"] for a in out["answers"]] == ["Yes, the fixture.", "test@example.com"]
    assert out["cover_letter"] == ""


def test_half_a_job_reference_is_rejected(mod, monkeypatch, tmp_path):
    rc, _ = _run(mod, {"date": "2026-09-25", "questions": []}, monkeypatch)
    assert rc == 2
