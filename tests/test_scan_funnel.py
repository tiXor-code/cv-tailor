# tests/test_scan_funnel.py
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
