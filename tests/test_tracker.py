"""Outcome tracking (cv_tailor.tracker, scripts/mark_outcome.py,
scripts/tracker_report.py) and the digest's "Follow up?" section.
All data fictional."""
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cv_tailor import cache
from cv_tailor.autopilot import AutopilotReport, build_digest, run_autopilot
from cv_tailor.tracker import (
    NotApplied, build_report, cv_variant, follow_ups_due, format_report, set_outcome,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def _entry(i, status="sent", applied="2026-09-20T10:00:00+00:00", **over):
    e = {"id": f"job-{i}", "status": status, "company": f"Fictco{i}",
         "title": f"AI Engineer {i}", "source": "ashby", "score": 8,
         "url": f"https://example.invalid/{i}"}
    if applied:
        e["applied_at"] = applied
    e.update(over)
    return e


def _write(root: Path, day: str, entries):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    (d / "jobs.json").write_text(json.dumps(entries))


def _read(root, day="2026-09-20"):
    return {e["id"]: e for e in json.loads((root / day / "jobs.json").read_text())}


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_QUEUE_DIR", str(tmp_path))
    monkeypatch.setenv("SCOUT_DB_PATH", str(tmp_path / "jobs.db"))
    return tmp_path


# --- set_outcome ---------------------------------------------------------------

def test_outcome_is_recorded_with_dated_history(q):
    _write(q, "2026-09-20", [_entry(1)])
    entry, prev = set_outcome("2026-09-20", "job-1", "replied", note="recruiter emailed",
                              now=NOW)
    assert prev == "no_reply"
    assert entry["outcome"] == "replied" and entry["outcome_at"] == NOW.isoformat()
    assert entry["outcome_history"] == [
        {"outcome": "replied", "at": NOW.isoformat(), "note": "recruiter emailed"}]
    later = datetime(2026, 10, 7, tzinfo=timezone.utc)
    entry, prev = set_outcome("2026-09-20", "job-1", "interview", now=later)
    assert prev == "replied"
    assert [h["outcome"] for h in entry["outcome_history"]] == ["replied", "interview"]
    assert _read(q)["job-1"]["outcome"] == "interview"
    # status is untouched: outcome is a separate axis
    assert _read(q)["job-1"]["status"] == "sent"


def test_only_applied_entries_take_an_outcome(q):
    _write(q, "2026-09-20", [_entry(1, status="handed_off")])
    with pytest.raises(NotApplied):
        set_outcome("2026-09-20", "job-1", "replied")
    assert "outcome" not in _read(q)["job-1"]


def test_unknown_outcome_is_refused(q):
    _write(q, "2026-09-20", [_entry(1)])
    with pytest.raises(ValueError):
        set_outcome("2026-09-20", "job-1", "ghosted")


def test_note_is_cleaned_and_capped(q):
    _write(q, "2026-09-20", [_entry(1)])
    entry, _ = set_outcome("2026-09-20", "job-1", "replied", note="a\x00b" + "x" * 900)
    note = entry["outcome_history"][0]["note"]
    assert "\x00" not in note and len(note) == 500


# --- follow-ups ----------------------------------------------------------------

def test_follow_ups_due_after_eight_days_freshest_first(q):
    _write(q, "2026-09-20", [
        _entry(1, applied="2026-09-28T10:00:00+00:00"),            # 7 days: not yet
        _entry(2, applied="2026-09-27T08:00:00+00:00"),            # 8 days
        _entry(3, status="applied_by_hand", applied="2026-09-01T08:00:00+00:00"),
        _entry(4, applied="2026-09-01T08:00:00+00:00", outcome="rejected"),
        _entry(5, status="handed_off"),
    ])
    due = follow_ups_due(NOW)
    assert [d["id"] for d in due] == ["job-2", "job-3"]
    assert due[0]["days_since_applied"] == 8
    assert due[1]["channel"] == "by_hand"


def test_followed_up_drops_it_off_the_list(q):
    _write(q, "2026-09-20", [_entry(2, applied="2026-09-20T08:00:00+00:00")])
    assert follow_ups_due(NOW)
    entry, _ = set_outcome("2026-09-20", "job-2", followed_up=True, now=NOW)
    assert entry["followed_up_at"] == NOW.isoformat()
    assert entry["outcome"] == "no_reply"
    assert follow_ups_due(NOW) == []


# --- report --------------------------------------------------------------------

def test_report_breakdowns(q, tmp_path):
    pkg = tmp_path / "pkg-a"
    pkg.mkdir()
    (pkg / "meta.json").write_text(json.dumps({"variant": "builder"}))
    _write(q, "2026-09-20", [
        _entry(1, package_dir=str(pkg), outcome="interview", score=9),
        _entry(2, status="applied_by_hand", source="linkedin", score=6),
        _entry(3, outcome="rejected", score=7),
        _entry(4, status="needs_human", applied=None, error="captcha",
               status_changed_at="2026-09-25T00:00:00+00:00"),
        _entry(5, status="rejected", applied=None),
    ])
    r = build_report(NOW)
    assert r["totals"] == {"entries": 5, "applied": 3, "responded": 2, "response_rate": 0.667}
    assert r["status_breakdown"]["sent"] == 2
    assert r["outcome_breakdown"] == {"no_reply": 1, "interview": 1, "rejected": 1}
    rr = r["response_rate"]
    assert rr["variant"]["builder"] == {"applied": 1, "responded": 1, "positive": 1,
                                        "response_rate": 1.0}
    assert rr["variant"]["cv"]["applied"] == 2
    assert rr["channel"]["scout"]["responded"] == 2
    assert rr["channel"]["by_hand"]["responded"] == 0
    assert rr["source"]["linkedin"]["applied"] == 1
    assert set(rr["score_band"]) == {"9-10", "7-8", "5-6"}
    assert [s["id"] for s in r["stale_waiting"]] == ["job-4"]
    assert r["stale_waiting"][0]["reason"] == "captcha"
    assert "Follow-ups due (1)" in format_report(r)


def test_cv_variant_falls_back_to_chosen_file_then_cv(tmp_path):
    pkg = tmp_path / "p"
    pkg.mkdir()
    (pkg / "meta.json").write_text(json.dumps({"variants": {"chosen": "cv_leadership.pdf"}}))
    assert cv_variant({"package_dir": str(pkg)}) == "cv_leadership"
    assert cv_variant({"cv_path": "/x/cv_ats.pdf"}) == "cv_ats"
    assert cv_variant({"cv_path": "/x/cv.pdf"}) == "cv"
    assert cv_variant({}) == "cv"


def test_tracker_report_script_writes_json(q, capsys):
    _write(q, "2026-09-20", [_entry(1)])
    mod = _load("tracker_report")
    out = q / "out" / "tracker.json"
    assert mod.main(["--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["totals"]["applied"] == 1
    assert "Scout tracker" in capsys.readouterr().out


def test_tracker_report_default_path_is_under_the_queue_root(q):
    _write(q, "2026-09-20", [_entry(1)])
    assert _load("tracker_report").main([]) == 0
    assert (q / "tracker.json").exists()


# --- mark_outcome CLI ------------------------------------------------------------

def test_mark_outcome_mirrors_into_the_ledger(q):
    _write(q, "2026-09-20", [_entry(1)])
    conn = cache.connect(q / "jobs.db")
    cache.record_application(conn, job_id="job-1", company="Fictco1", role="AI Engineer 1",
                             url="https://example.invalid/1", channel="portal")
    conn.close()
    mod = _load("mark_outcome")
    assert mod.main(["2026-09-20", "job-1", "rejected", "--note", "form email"]) == 0
    row = sqlite3.connect(q / "jobs.db").execute(
        "SELECT outcome, outcome_at FROM applications WHERE job_id='job-1'").fetchone()
    assert row[0] == "rejected" and row[1]
    assert _read(q)["job-1"]["outcome_history"][0]["note"] == "form email"


def test_ledger_migration_adds_outcome_columns_to_an_old_db(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE applications (job_id TEXT PRIMARY KEY, company TEXT, role TEXT,"
              " norm_key TEXT, url TEXT, channel TEXT, sent_at TEXT)")
    c.execute("INSERT INTO applications VALUES ('j','C','R','c|r','u','portal','2026-09-01')")
    c.commit()
    c.close()
    conn = cache.connect(db)
    conn = cache.connect(db)  # idempotent
    assert cache.set_application_outcome(conn, job_id="j", outcome="offer", at="t")
    assert not cache.set_application_outcome(conn, job_id="missing", outcome="offer", at="t")


def test_mark_outcome_exit_codes(q):
    _write(q, "2026-09-20", [_entry(1), _entry(2, status="handed_off")])
    mod = _load("mark_outcome")
    assert mod.main(["2026-09-20", "job-2", "replied"]) == 3
    assert mod.main(["2026-09-20", "nope", "replied"]) == 4
    assert mod.main(["2026-09-20", "job-1", "ghosted"]) == 2
    assert mod.main(["2026-09-20", "job-1"]) == 2
    assert mod.main(["../etc", "job-1", "replied"]) == 2
    assert mod.main(["2026-09-20", "job-1", "--followed-up"]) == 0
    assert _read(q)["job-1"]["followed_up_at"]


def test_interview_triggers_prep_once_and_a_prep_failure_keeps_the_outcome(q, monkeypatch):
    _write(q, "2026-09-20", [_entry(1), _entry(2)])
    mod = _load("mark_outcome")
    calls = []
    monkeypatch.setattr(mod, "run_prep", lambda d, j: calls.append((d, j)) or Path("/x/p.md"))
    assert mod.main(["2026-09-20", "job-1", "interview"]) == 0
    assert calls == [("2026-09-20", "job-1")]
    assert mod.main(["2026-09-20", "job-1", "interview", "--note", "round 2"]) == 0
    assert len(calls) == 1  # already at interview: no second pack
    assert mod.main(["2026-09-20", "job-2", "interview", "--no-prep"]) == 0
    assert len(calls) == 1

    def boom(d, j):
        raise RuntimeError("llm down")
    monkeypatch.setattr(mod, "run_prep", boom)
    _write(q, "2026-09-21", [_entry(3)])
    assert mod.main(["2026-09-21", "job-3", "interview"]) == 0
    assert _read(q, "2026-09-21")["job-3"]["outcome"] == "interview"


# --- digest -------------------------------------------------------------------

def test_digest_lists_follow_ups_capped(q):
    _write(q, "2026-09-20", [_entry(i, applied="2026-09-20T08:00:00+00:00") for i in range(7)])
    report = run_autopilot(NOW, runner=lambda d, j: 0, notify=None,
                           ledger_has=lambda j: False)
    assert len(report.follow_ups) == 5 and report.follow_ups_total == 7
    report.queued_new.append(("2026-10-05", {"company": "X", "title": "Y", "score": 6}))
    text = build_digest(report)
    assert "Follow up? (5 of 7):" in text
    assert "Fictco0 / AI Engineer 0: 15 days since applied, no reply" in text


def test_follow_ups_alone_do_not_send_a_digest():
    r = AutopilotReport(follow_ups=[{"company": "A", "title": "B", "days_since_applied": 9}],
                        follow_ups_total=1)
    assert build_digest(r) is None
