"""update_entry stamps status_changed_at on real status transitions only."""
import json
from datetime import datetime, timezone
from pathlib import Path

from cv_tailor.scout_queue import update_entry


def _seed(tmp_path: Path, status="pending") -> Path:
    day = tmp_path / "2026-07-23"
    day.mkdir(parents=True)
    (day / "jobs.json").write_text(json.dumps([{
        "id": "job-1", "title": "AI Engineer", "company": "ExampleCo",
        "score": 8, "status": status, "decided_at": None,
    }]))
    return tmp_path


def test_status_change_stamps_timestamp(tmp_path):
    root = _seed(tmp_path)
    before = datetime.now(timezone.utc)
    entry = update_entry("2026-07-23", "job-1",
                          lambda e: e.update(status="approved"), queue_dir=root)
    stamped = datetime.fromisoformat(entry["status_changed_at"])
    assert stamped.tzinfo is not None
    assert stamped >= before.replace(microsecond=0)
    on_disk = json.loads((root / "2026-07-23" / "jobs.json").read_text())[0]
    assert on_disk["status_changed_at"] == entry["status_changed_at"]


def test_non_status_write_does_not_stamp(tmp_path):
    root = _seed(tmp_path)
    entry = update_entry("2026-07-23", "job-1",
                          lambda e: e.update(cv_path="/tmp/cv.pdf"), queue_dir=root)
    assert "status_changed_at" not in entry


def test_same_status_write_does_not_stamp(tmp_path):
    root = _seed(tmp_path)
    entry = update_entry("2026-07-23", "job-1",
                          lambda e: e.update(status="pending"), queue_dir=root)
    assert "status_changed_at" not in entry
