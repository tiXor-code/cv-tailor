"""Interview prep: every story and defence must be grounded in the profile item
it cites; the rest becomes an honest gap. LLM is mocked; data fictional."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cv_tailor import interview_prep as ip

ROOT = Path(__file__).resolve().parents[1]

PROFILE = {
    "contact": {"name": "Ana Example"},
    "summary_pool": [{"id": "s", "text": "Builder."}],
    "experiences": [
        {"id": "acme", "role": "Automation Lead", "company": "Acme Widgets",
         "dates": "Jan 2024 – Present",
         "bullets": ["Cut invoice processing from 9 days to 2 with an n8n pipeline.",
                     "Ran a 3-person delivery team."]},
    ],
    "projects": [
        {"id": "botty", "name": "Botty", "bullets": ["RAG support bot with 120 tests."]},
    ],
    "education": [{"degree": "BSc Things", "institution": "Example U", "year": 2019}],
    "skills": {"ai": ["RAG"]},
}
ANSWERS = {"salary_fulltime_gross_eur_month": 3100}


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=json.dumps(self.payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


RAW = {
    "questions": [
        {"question": "Tell me about an automation you shipped.", "why": "posting: automation",
         "source_id": "acme", "situation": "Invoices took 9 days.", "task": "Speed it up.",
         "action": "Built an n8n pipeline.", "result": "Down to 2 days."},
        {"question": "How big a team have you led?", "why": "posting: leadership",
         "source_id": "acme", "situation": "Team of 12.", "task": "Lead.",
         "action": "Led.", "result": "Shipped."},   # 12 is invented
        {"question": "Kubernetes at scale?", "why": "posting: k8s", "source_id": None},
        {"question": "Testing?", "why": "posting: quality", "source_id": "ghost",
         "situation": "x", "task": "y", "action": "z", "result": "w"},
    ],
    "probes": [
        {"claim": "Cut invoice processing from 9 days to 2", "source_id": "acme",
         "defence": "Measured on the 2024 invoice runs."},  # 2024 is in the dates
        {"claim": "120 tests", "source_id": "botty", "defence": "Saved 40 hours."},  # 40 invented
    ],
    "gaps": [{"topic": "Go", "note": "Not in the profile; say so."}],
}


def test_validate_keeps_grounded_stories_and_demotes_the_rest():
    pack = ip.validate(RAW, PROFILE)
    q = pack["questions"]
    assert q[0]["star"]["result"] == "Down to 2 days." and q[0]["source_id"] == "acme"
    assert q[1]["star"] is None and q[2]["star"] is None and q[3]["star"] is None
    topics = [g["topic"] for g in pack["gaps"]]
    assert "Go" in topics and "How big a team have you led?" in topics
    assert "Kubernetes at scale?" in topics
    assert [p["source_id"] for p in pack["probes"]] == ["acme"]
    reasons = " ".join(d["reason"] for d in pack["dropped"])
    assert "12" in reasons and "40" in reasons and "ghost" in reasons


def test_questions_and_probes_are_capped():
    raw = {"questions": [{"question": f"Q{i}?"} for i in range(30)],
           "probes": [{"claim": "Ran a 3-person delivery team.", "source_id": "acme",
                       "defence": "ok"}] * 9}
    pack = ip.validate(raw, PROFILE)
    assert len(pack["questions"]) == ip.MAX_QUESTIONS
    assert len(pack["probes"]) == ip.MAX_PROBES


def test_markdown_has_rehearsal_from_floors_and_marks_unknown_contractor_floor():
    pack = ip.validate(RAW, PROFILE)
    md = ip.to_markdown(pack, company="Fictco", role="AI Engineer", profile=PROFILE,
                        answers=ANSWERS, jd_missing=False)
    assert "walk-away 3,100 EUR/month" in md
    assert "Contractor (invoiced): unknown, `contractor_floor_eur_month`" in md
    assert "**Situation:** Invoices took 9 days." in md
    assert "Team of 12" not in md
    md2 = ip.to_markdown(pack, company="F", role="R", profile=PROFILE, answers={},
                         jd_missing=True)
    assert "Employee (gross): unknown" in md2 and "not archived" in md2


def _queue(tmp_path, entry):
    d = tmp_path / "2026-09-20"
    d.mkdir(parents=True)
    (d / "jobs.json").write_text(json.dumps([entry]))
    (d / "descriptions.json").write_text(json.dumps({entry["id"]: "We need automation and RAG."}))


def test_prepare_writes_into_the_package_dir(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "cover_letter.md").write_text("Dear team, I cut invoice time.")
    _queue(tmp_path, {"id": "j1", "status": "sent", "company": "Fictco", "title": "AI Eng",
                      "package_dir": str(pkg)})
    client = FakeClient(RAW)
    out = ip.prepare("2026-09-20", "j1", profile=PROFILE, answers=ANSWERS, client=client,
                     queue_dir=tmp_path)
    assert out == pkg / "interview_prep.md" and out.exists()
    assert (pkg / "interview_prep.json").exists()
    user_msg = client.calls[0]["messages"][1]["content"]
    assert "We need automation and RAG." in user_msg and "I cut invoice time" in user_msg
    # existing pack is kept without --force
    ip.prepare("2026-09-20", "j1", profile=PROFILE, answers=ANSWERS, client=client,
               queue_dir=tmp_path)
    assert len(client.calls) == 1


def test_prepare_without_a_package_uses_the_assemble_location(tmp_path):
    _queue(tmp_path, {"id": "j1", "status": "applied_by_hand", "company": "Fict Co",
                      "title": "AI Eng"})
    out = ip.prepare("2026-09-20", "j1", profile=PROFILE, answers=ANSWERS,
                     client=FakeClient(RAW), queue_dir=tmp_path)
    assert out.parent.parent == tmp_path / "2026-09-20" / "packages"
    assert out.parent.name == "2026-09-20-fict-co-ai-eng"


def test_script_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    prof = tmp_path / "profile.yaml"
    import yaml
    prof.write_text(yaml.safe_dump(PROFILE))
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(prof))
    monkeypatch.setenv("CV_TAILOR_ANSWERS", str(tmp_path / "none.yaml"))
    _queue(tmp_path, {"id": "j1", "status": "sent", "company": "F", "title": "R"})
    spec = importlib.util.spec_from_file_location("interview_prep_cli",
                                                  ROOT / "scripts" / "interview_prep.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["interview_prep_cli"] = m
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "_load_dotenv", lambda *a, **k: None)
    assert m.main(["2026-09-20", "j1"], client=FakeClient(RAW)) == 0
    assert m.main(["2026-09-20", "nope"], client=FakeClient(RAW)) == 4
    assert m.main(["bad-date", "j1"], client=FakeClient(RAW)) == 2
