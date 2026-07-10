# tests/test_cache.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.cache import (
    connect, is_new, mark_seen,
    record_application, delete_application, application_exists, applications_sent_today,
    own_application_recorded,
)
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


def test_application_record_then_exists_by_job_id(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    assert application_exists(conn, job_id="job-1", company="Acme", role="AI Engineer") is False
    record_application(conn, job_id="job-1", company="Acme", role="AI Engineer",
                        url="https://x/1", channel="email")
    assert application_exists(conn, job_id="job-1", company="Acme", role="AI Engineer") is True


def test_application_exists_by_normalized_company_role_different_id(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="job-1", company="Acme Inc.", role="AI Engineer",
                        url="https://x/1", channel="email")
    # Different job_id, same company/role after normalization -> already applied.
    assert application_exists(conn, job_id="job-2", company="acme inc", role="AI  Engineer") is True


def test_application_exists_false_for_different_company_role(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="job-1", company="Acme", role="AI Engineer",
                        url="https://x/1", channel="email")
    assert application_exists(conn, job_id="job-3", company="Beta Corp", role="Backend Engineer") is False


def test_applications_sent_today_counts_todays_rows(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    assert applications_sent_today(conn) == 0
    record_application(conn, job_id="job-1", company="Acme", role="AI Engineer",
                        url="https://x/1", channel="email")
    assert applications_sent_today(conn) == 1


def test_record_application_returns_true_on_first_insert(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    assert record_application(
        conn, job_id="job-1", company="Acme", role="AI Engineer",
        url="https://x/1", channel="email",
    ) is True


def test_record_application_second_insert_same_job_id_returns_false(tmp_path):
    """PRIMARY KEY(job_id) arbitrates a same-job_id race: the second insert
    for the same job_id must not silently overwrite the first (no more
    INSERT OR REPLACE) -- it must fail and report False so the caller can
    block instead of double-sending."""
    conn = connect(tmp_path / "jobs.db")
    assert record_application(
        conn, job_id="job-1", company="Acme", role="AI Engineer",
        url="https://x/1", channel="email",
    ) is True
    assert record_application(
        conn, job_id="job-1", company="Acme", role="AI Engineer",
        url="https://x/1", channel="email",
    ) is False
    row = conn.execute("SELECT COUNT(*) FROM applications WHERE job_id='job-1'").fetchone()
    assert row[0] == 1


def test_delete_application_removes_the_row(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="job-1", company="Acme", role="AI Engineer",
                        url="https://x/1", channel="email")
    assert application_exists(conn, job_id="job-1", company="Acme", role="AI Engineer") is True

    delete_application(conn, job_id="job-1")

    assert application_exists(conn, job_id="job-1", company="Acme", role="AI Engineer") is False


def test_delete_application_missing_job_id_is_a_noop(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    delete_application(conn, job_id="does-not-exist")  # must not raise


def test_own_application_recorded_false_when_no_row(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    assert own_application_recorded(conn, "job-1") is False


def test_own_application_recorded_true_only_for_the_exact_job_id(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="job-1", company="Acme", role="AI Engineer",
                        url="https://x/1", channel="portal")

    assert own_application_recorded(conn, "job-1") is True
    # A different job_id -- even with the same normalized company|role -- is
    # NOT "our own" row (that's application_exists's job, not this one's).
    assert own_application_recorded(conn, "job-2") is False
