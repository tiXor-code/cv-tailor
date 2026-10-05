"""scripts/park_job.py: the autopilot parks what it cannot finish, with why."""
import importlib.util
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(monkeypatch, tmp_path, payload):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("park_job", ROOT / "scripts" / "park_job.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    return m.main()


def test_parks_with_reason_and_capped_questions(monkeypatch, tmp_path):
    day = tmp_path / "2026-10-05"
    day.mkdir()
    (day / "jobs.json").write_text(json.dumps([{"id": "job-1", "status": "handed_off"}]))
    rc = _run(monkeypatch, tmp_path, {"date": "2026-10-05", "id": "job-1", "reason": "Needs your answer\x07",
                                      "questions": ["What is your notice period?"] * 30})
    assert rc == 0
    e = json.loads((day / "jobs.json").read_text())[0]
    assert e["status"] == "handed_off" and e["park_reason"] == "Needs your answer"
    assert len(e["park_questions"]) == 20 and e["parked_at"]


def test_rejects_bad_input_and_unknown_jobs(monkeypatch, tmp_path):
    (tmp_path / "2026-10-05").mkdir()
    (tmp_path / "2026-10-05" / "jobs.json").write_text("[]")
    assert _run(monkeypatch, tmp_path, {"date": "../x", "id": "j", "reason": "r"}) == 2
    assert _run(monkeypatch, tmp_path, {"date": "2026-10-05", "id": "j", "reason": ""}) == 2
    assert _run(monkeypatch, tmp_path, {"date": "2026-10-05", "id": "nope", "reason": "r"}) == 4
