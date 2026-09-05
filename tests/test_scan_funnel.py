# tests/test_scan_funnel.py
import json
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import scan
from cv_tailor.cache import connect
from cv_tailor.job_sources import JobPosting


def _job(source, raw_id, title, location, desc=""):
    return JobPosting(source=source, org=f"Co{raw_id}", title=title, location=location,
                      url="https://x", description=desc, raw_id=raw_id)


def test_funnel_filters_and_dedupes(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    tracks = {"ai": {"keywords": ["ai engineer", "python"]}}
    jobs = [
        _job("greenhouse", "1", "AI Engineer", "Remote - EU", "Python"),        # passes
        _job("greenhouse", "2", "Account Executive", "Remote - EU", "sales"),   # fails gate1 (role)
        _job("greenhouse", "3", "AI Engineer", "Remote - US only", "Python"),   # fails gate1 (geo)
        _job("workday",    "4", "AI Engineer", "Remote - EU", "Python"),        # fails gate2 (enterprise)
    ]
    survivors = scan.run_gates(jobs, tracks, conn)
    assert [j.raw_id for j in survivors] == ["1"]
    assert survivors[0].track == "ai"

    # Mark #1 seen, re-run: now deduped out.
    from cv_tailor.cache import mark_seen
    mark_seen(conn, jobs[0], score=9)
    assert scan.run_gates(jobs, tracks, conn) == []


def test_funnel_tags_winning_track():
    conn = connect(":memory:")
    tracks = {
        "ai": {"keywords": ["ai engineer"]},
        "content": {"keywords": ["content producer"]},
    }
    jobs = [
        _job("greenhouse", "5", "Content Producer", "Remote - EU", "content producer role"),
        _job("greenhouse", "6", "AI Content Producer", "Remote - EU", "ai engineer and content producer"),
    ]
    survivors = scan.run_gates(jobs, tracks, conn)
    by_id = {j.raw_id: j for j in survivors}
    assert by_id["5"].track == "content"
    assert by_id["6"].track == "ai"  # tie -> ai wins config order


def test_quiet_digest_decides_send():
    assert scan.should_send([]) is False
    assert scan.should_send([{"score": 8}]) is True


def test_drop_crm_tracked():
    jobs = [
        _job("greenhouse", "1", "AI Engineer", "Remote - EU"),
        _job("lever", "2", "Backend Engineer", "Remote - EU"),
    ]
    # "Co1" / "AI Engineer" already tracked (normalized) -> dropped; whitespace/case-insensitive.
    tracked = {("co1", "aiengineer")}
    kept = scan.drop_crm_tracked(jobs, tracked)
    assert [j.raw_id for j in kept] == ["2"]
    # empty tracked set keeps everything
    assert len(scan.drop_crm_tracked(jobs, set())) == 2


# --- auto_apply_pending -------------------------------------------------

def _write_day_queue(queue_dir, scan_date, entries):
    day_dir = queue_dir / scan_date
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "jobs.json").write_text(json.dumps(entries))
    return day_dir


def _read_day_queue(queue_dir, scan_date):
    return json.loads((queue_dir / scan_date / "jobs.json").read_text())


def test_auto_apply_pending_approves_and_runs_in_order(tmp_path):
    scan_date = "2026-07-10"
    entries = [
        {"id": "a", "company": "Acme", "status": "pending"},
        {"id": "b", "company": "Beta", "status": "pending"},
    ]
    _write_day_queue(tmp_path, scan_date, entries)

    calls = []

    def fake_runner(scan_date_iso, entry_id, log_path):
        calls.append((scan_date_iso, entry_id))
        return 0

    results = scan.auto_apply_pending(scan_date, queue_dir=tmp_path, runner=fake_runner)

    assert calls == [(scan_date, "a"), (scan_date, "b")]
    assert results == [("a", 0), ("b", 0)]

    updated = _read_day_queue(tmp_path, scan_date)
    for e in updated:
        assert e["status"] == "approved"
        assert e["decided_by"] == "auto"
        assert e["decided_at"] is not None


def test_auto_apply_pending_skips_non_pending(tmp_path):
    scan_date = "2026-07-10"
    entries = [
        {"id": "a", "company": "Acme", "status": "approved"},
        {"id": "b", "company": "Beta", "status": "pending"},
        {"id": "c", "company": "Gamma", "status": "rejected"},
    ]
    _write_day_queue(tmp_path, scan_date, entries)

    calls = []

    def fake_runner(scan_date_iso, entry_id, log_path):
        calls.append(entry_id)
        return 0

    results = scan.auto_apply_pending(scan_date, queue_dir=tmp_path, runner=fake_runner)

    assert calls == ["b"]
    assert results == [("b", 0)]

    updated = {e["id"]: e for e in _read_day_queue(tmp_path, scan_date)}
    assert updated["a"]["status"] == "approved"  # untouched, was already
    assert "decided_by" not in updated["a"]
    assert updated["c"]["status"] == "rejected"  # untouched


def test_auto_apply_pending_continues_after_runner_failure(tmp_path):
    scan_date = "2026-07-10"
    entries = [
        {"id": "a", "company": "Acme", "status": "pending"},
        {"id": "b", "company": "Beta", "status": "pending"},
    ]
    _write_day_queue(tmp_path, scan_date, entries)

    calls = []

    def flaky_runner(scan_date_iso, entry_id, log_path):
        calls.append(entry_id)
        if entry_id == "a":
            raise TimeoutError("boom")
        return 0

    results = scan.auto_apply_pending(scan_date, queue_dir=tmp_path, runner=flaky_runner)

    # both jobs ran despite the first raising -- the failure did not stop the loop
    assert calls == ["a", "b"]
    assert results == [("a", -1), ("b", 0)]


def test_auto_apply_pending_no_pending_returns_empty(tmp_path):
    scan_date = "2026-07-10"
    entries = [{"id": "a", "company": "Acme", "status": "approved"}]
    _write_day_queue(tmp_path, scan_date, entries)

    calls = []
    results = scan.auto_apply_pending(
        scan_date, queue_dir=tmp_path, runner=lambda *a: calls.append(a) or 0
    )
    assert results == []
    assert calls == []


def test_auto_apply_enabled_gate(monkeypatch):
    monkeypatch.delenv("AUTO_APPLY", raising=False)
    assert scan._auto_apply_enabled(dry_run=False) is False
    assert scan._auto_apply_enabled(dry_run=True) is False

    monkeypatch.setenv("AUTO_APPLY", "1")
    assert scan._auto_apply_enabled(dry_run=False) is True
    assert scan._auto_apply_enabled(dry_run=True) is False  # dry-run always wins

    monkeypatch.setenv("AUTO_APPLY", "0")
    assert scan._auto_apply_enabled(dry_run=False) is False


def test_funnel_dedupes_same_norm_key_within_one_batch(tmp_path):
    """2026-07-10 regression: 4 regional variants of one Remote.com role share a
    norm_key (region is stripped) and all passed Gate 3 in a single scan, so all
    four queued and the first attempt's ledger row blocked the other three as
    "duplicate". Only ONE variant may survive a batch."""
    conn = connect(tmp_path / "jobs.db")
    tracks = {"ai": {"keywords": ["ai engineer", "python"]}}
    variants = [
        _job("greenhouse", "r1", "AI Engineer", "Remote - EMEA", "Python"),
        _job("greenhouse", "r2", "AI Engineer", "Remote - Northern EU", "Python"),
        _job("greenhouse", "r3", "AI Engineer", "Remote - Southern EU", "Python"),
    ]
    # same org so the norm_key collides
    for v in variants:
        v.org = "Remote.com"
    survivors = scan.run_gates(variants, tracks, conn)
    assert len(survivors) == 1
    assert survivors[0].raw_id == "r1"
