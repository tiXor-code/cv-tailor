"""Autopilot policy: select >=8, CAS approve, run orchestrator, expire, digest.

The orchestrator is injected as `runner(scan_date, job_id) -> int` so no real
subprocess/browser/SMTP ever runs here; the fake runner mutates the queue the
way scripts/apply_approved.py would (status writes via update_entry).
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cv_tailor.autopilot import (
    AUTO_APPROVE_MIN, EXPIRE_DAYS, AutopilotReport, build_digest, run_autopilot,
)
from cv_tailor.scout_queue import update_entry

NOW = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
TODAY = "2026-07-23"


def _write_day(root: Path, day: str, entries: list[dict]) -> None:
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    (d / "jobs.json").write_text(json.dumps(entries))


def _entry(i, score=8, status="pending", **over):
    base = {"id": f"job-{i}", "title": f"Role {i}", "company": f"Co{i}",
            "score": score, "status": status, "decided_at": None,
            "apply_method": "email", "apply_target": "hr@example.invalid",
            "url": f"https://example.invalid/{i}"}
    base.update(over)
    return base


def _read(root, day):
    return {e["id"]: e for e in json.loads((root / day / "jobs.json").read_text())}


def test_approves_only_eight_plus_highest_first(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=7), _entry(2, score=8), _entry(3, score=9)])
    ran = []

    def runner(day, job_id):
        ran.append(job_id)
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert ran == ["job-3", "job-2"]  # score desc; the 7 untouched
    q = _read(tmp_path, TODAY)
    assert q["job-1"]["status"] == "pending"
    assert q["job-2"]["approved_by"] == "autopilot"
    assert q["job-2"]["decided_at"] is not None
    assert [e["id"] for _, e in report.applied] == ["job-3", "job-2"]
    assert [e["id"] for _, e in report.queued_new] == ["job-1"]


def test_cas_conflict_skips_without_crash(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9)])

    calls = []

    def runner(day, job_id):  # pragma: no cover - must never run
        calls.append(job_id)
        return 0

    # Simulate Teodor's tap racing autopilot: entry already approved.
    update_entry(TODAY, "job-1", lambda e: e.update(status="approved"), queue_dir=tmp_path)
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert calls == []
    assert report.applied == [] and report.failed == []


def test_outcome_bucketing(tmp_path):
    _write_day(tmp_path, TODAY, [
        _entry(1, score=9), _entry(2, score=8), _entry(3, score=8), _entry(4, score=8)])
    outcome = {"job-1": "sent", "job-2": "needs_human", "job-3": "needs_review", "job-4": "failed"}

    def runner(day, job_id):
        update_entry(day, job_id, lambda e: e.update(status=outcome[job_id]), queue_dir=tmp_path)
        return 0 if outcome[job_id] != "failed" else 1

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert [e["id"] for _, e in report.applied] == ["job-1"]
    assert sorted(e["id"] for _, e in report.parked) == ["job-2", "job-3"]
    assert [e["id"] for _, e in report.failed] == ["job-4"]


def test_runner_exception_is_recorded_not_raised(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9)])

    def runner(day, job_id):
        raise RuntimeError("boom")

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert [e["id"] for _, e in report.failed] == ["job-1"]


def test_expiry_sweep_boundary_and_statuses(tmp_path):
    old_day = (NOW - timedelta(days=8)).date().isoformat()
    edge_day = (NOW - timedelta(days=6)).date().isoformat()
    stale = NOW - timedelta(days=7, hours=1)
    fresh = NOW - timedelta(days=6)
    _write_day(tmp_path, old_day, [
        _entry(1, status="pending"),                                        # no stamp -> scan date -> expired
        _entry(2, status="needs_human", status_changed_at=stale.isoformat()),   # expired
        _entry(3, status="needs_review", status_changed_at=fresh.isoformat()),  # kept (recent change)
        _entry(4, status="sent"),                                           # terminal -> never expired
    ])
    _write_day(tmp_path, edge_day, [_entry(5, score=7, status="pending")])  # 6d old, score 7 -> kept, not auto-approved
    _write_day(tmp_path, TODAY, [])

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)
    assert sorted(e["id"] for _, e in report.expired) == ["job-1", "job-2"]
    q_old = _read(tmp_path, old_day)
    assert q_old["job-1"]["status"] == "rejected" and q_old["job-1"]["error"] == "auto_expired"
    assert q_old["job-3"]["status"] == "needs_review"
    assert q_old["job-4"]["status"] == "sent"
    assert _read(tmp_path, edge_day)["job-5"]["status"] == "pending"


def test_backlog_eights_within_window_are_approved(tmp_path):
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [_entry(1, score=8)])
    _write_day(tmp_path, TODAY, [])
    ran = []

    def runner(day, job_id):
        ran.append((day, job_id))
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    assert ran == [(yesterday, "job-1")]
    assert [e["id"] for _, e in report.applied] == ["job-1"]


def test_queued_new_counts_only_todays_pendings(tmp_path):
    yesterday = (NOW - timedelta(days=1)).date().isoformat()
    _write_day(tmp_path, yesterday, [_entry(1, score=7)])   # old 7: not "new"
    _write_day(tmp_path, TODAY, [_entry(2, score=7)])
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)
    assert [e["id"] for _, e in report.queued_new] == ["job-2"]
    assert report.has_activity()  # a new 7 queued for review IS activity


def test_no_activity_no_digest(tmp_path):
    _write_day(tmp_path, TODAY, [])
    report = run_autopilot(NOW, queue_dir=tmp_path, runner=lambda d, j: 0)
    assert not report.has_activity()
    assert build_digest(report) is None


def test_digest_contents(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9), _entry(2, score=7)])

    def runner(day, job_id):
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    report = run_autopilot(NOW, queue_dir=tmp_path, runner=runner)
    text = build_digest(report)
    assert "Co1" in text and "Role 1" in text
    assert "Co2" in text                       # queued 7
    assert "admin.teodorlutoiu.com/scout" in text
    assert "—" not in text and "–" not in text  # no em/en dash


def test_notify_called_only_with_activity(tmp_path):
    _write_day(tmp_path, TODAY, [_entry(1, score=9)])
    sent = []

    def runner(day, job_id):
        update_entry(day, job_id, lambda e: e.update(status="sent"), queue_dir=tmp_path)
        return 0

    run_autopilot(NOW, queue_dir=tmp_path, runner=runner, notify=lambda t: sent.append(t) or True)
    assert len(sent) == 1

    sent2 = []
    _write_day(tmp_path, "2026-07-24", [])
    run_autopilot(NOW + timedelta(days=1), queue_dir=tmp_path, runner=lambda d, j: 0,
                  notify=lambda t: sent2.append(t) or True)
    assert sent2 == []
