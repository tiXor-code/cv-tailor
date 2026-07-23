"""Autopilot-approved entries through the apply_approved state machine.

Pins the spec's letter gate: residual cover warnings (which already survived
cover_llm's internal feed-warnings-back retry loop, MAX_ATTEMPTS=3) park the
job at needs_review and never send -- for autopilot approvals exactly like
manual ones. Also pins that approved_by survives the status trail.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "apply_approved.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("apply_approved_autopilot", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    day = tmp_path / "2026-07-23"
    day.mkdir()
    (day / "jobs.json").write_text(json.dumps([{
        "id": "job-1", "title": "AI Engineer", "company": "ExampleCo",
        "score": 9, "status": "approved", "approved_by": "autopilot",
        "decided_at": "2026-07-23T08:00:00+00:00",
        "apply_method": "email", "apply_target": "hr@example.invalid",
        "url": "https://example.invalid/1", "package_dir": None,
        "cv_path": None, "cover_letter_path": None,
    }]))
    return tmp_path


def _read(root):
    return json.loads((root / "2026-07-23" / "jobs.json").read_text())[0]


def test_residual_warnings_park_at_needs_review_and_never_send(queue, monkeypatch):
    mod = _load_module()
    mod.assemble_package = lambda entry, scan_date: {
        "package_dir": "/tmp/pkg", "cv_path": "/tmp/pkg/cv.pdf",
        "cover_letter_path": "/tmp/pkg/cover.txt",
        "cover_letter_warnings": ["banned phrase: 'passionate'"],
    }
    sends = []
    mod.send_application = lambda *a, **k: sends.append(1)
    mod.send_text = lambda *a, **k: True
    rc = mod.main(["2026-07-23", "job-1"])
    assert rc == 0
    entry = _read(queue)
    assert entry["status"] == "needs_review"
    assert entry["warnings"] == ["banned phrase: 'passionate'"]
    assert entry["approved_by"] == "autopilot"   # tag survives the trail
    assert sends == []


def test_clean_letter_proceeds_to_send(queue, monkeypatch):
    mod = _load_module()
    mod.assemble_package = lambda entry, scan_date: {
        "package_dir": "/tmp/pkg", "cv_path": "/tmp/pkg/cv.pdf",
        "cover_letter_path": "/tmp/pkg/cover.txt", "cover_letter_warnings": [],
    }

    class R:
        status = "preview_sent"

    mod.load_profile = lambda *a, **k: {"name": "Fake"}
    mod.connect = lambda *a, **k: None
    mod.send_application = lambda *a, **k: R()
    mod.send_text = lambda *a, **k: True
    rc = mod.main(["2026-07-23", "job-1"])
    assert rc == 0
    assert _read(queue)["status"] == "preview_sent"


def test_cover_llm_retry_loop_feeds_warnings_back():
    """The spec's 'regenerate once' lives in cover_llm: attempt 2+ must receive
    the prior warnings and prior draft in the prompt."""
    from cv_tailor import cover_llm

    calls = []

    class FakeResp:
        def __init__(self, text):
            self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]

    class FakeClient:
        class chat:  # noqa: N801 - mirrors openai client shape
            class completions:  # noqa: N801
                @staticmethod
                def create(model, messages, temperature):
                    calls.append(messages)
                    if len(calls) == 1:
                        return FakeResp("I am passionate about this role. " + "word " * 120)
                    return FakeResp("Plain honest letter. " + "word " * 120)

    text = cover_llm.cover_letter({"name": "Fake"}, "JD text", {}, client=FakeClient())
    assert len(calls) >= 2                       # it retried
    retry_prompt = str(calls[1])
    assert "passionate" in retry_prompt.lower()  # warnings fed back
    assert "Plain honest letter" in text
