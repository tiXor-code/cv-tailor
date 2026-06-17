# tests/test_cache.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.cache import connect, is_new, mark_seen
from cv_tailor.job_sources import JobPosting


def _job(source="greenhouse", raw_id="1", org="Acme", title="AI Engineer"):
    return JobPosting(source=source, org=org, title=title, location="Remote (EU)",
                      url="https://x", description="desc", raw_id=raw_id)


def test_new_then_seen(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    j = _job()
    assert is_new(conn, j) is True
    mark_seen(conn, j, score=8)
    assert is_new(conn, j) is False


def test_cross_source_dedup_by_company_role(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    a = _job(source="greenhouse", raw_id="1", org="Acme Inc.", title="AI Engineer")
    mark_seen(conn, a, score=8)
    # Same company+role from a different board / id is NOT new.
    b = _job(source="serpapi", raw_id="zzz", org="acme inc", title="AI  Engineer")
    assert is_new(conn, b) is False


def test_enrichment_roundtrip(tmp_path):
    from cv_tailor.cache import connect, get_enrichment, put_enrichment
    conn = connect(tmp_path / "jobs.db")
    assert get_enrichment(conn, "acme.com") is None
    put_enrichment(conn, "acme.com", is_smb=True, headcount="11-50", signal="hunter")
    row = get_enrichment(conn, "acme.com")
    assert row["is_smb"] is True and row["headcount"] == "11-50" and row["signal"] == "hunter"


def test_enrichment_ttl(tmp_path):
    from cv_tailor.cache import connect, get_enrichment, put_enrichment
    conn = connect(tmp_path / "jobs.db")
    put_enrichment(conn, "old.com", is_smb=False, headcount="5001-10000", signal="hunter")
    # max_age_days=0 means anything is stale -> treated as a miss
    assert get_enrichment(conn, "old.com", max_age_days=0) is None
    assert get_enrichment(conn, "old.com") is not None  # default window: hit
