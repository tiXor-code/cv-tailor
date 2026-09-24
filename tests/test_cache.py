# tests/test_cache.py
import sqlite3
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


# The seen_jobs table exactly as it shipped, i.e. the shape of the real
# data/jobs.db (1,241 rows on 2026-08-17) that any migration must survive.
_ORIGINAL_SEEN_JOBS = """
CREATE TABLE seen_jobs (
    source TEXT, raw_id TEXT, company TEXT, role TEXT, location TEXT,
    norm_key TEXT, first_seen TEXT, score INTEGER, status TEXT,
    PRIMARY KEY (source, raw_id)
);
"""


def _legacy_db(path) -> None:
    """A pre-migration jobs.db with one row, built from the original DDL."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_ORIGINAL_SEEN_JOBS)
    conn.execute(
        "INSERT INTO seen_jobs "
        "(source, raw_id, company, role, location, norm_key, first_seen, score, status) "
        "VALUES ('greenhouse','old-1','Acme','AI Engineer','Remote (EU)',"
        "'acme|aiengineer','2026-07-01T00:00:00Z',6,'scored')"
    )
    conn.commit()
    conn.close()


def _columns(conn, table="seen_jobs") -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


# --- Task 4: url + description persisted so a score can be revisited --------

def test_mark_seen_persists_url_and_description(tmp_path):
    """The 18 score-6 rows sitting in the live DB cannot be re-scored because
    seen_jobs kept neither the posting URL nor its text. Both are in scope at
    the single mark_seen call site, so both get stored."""
    conn = connect(tmp_path / "jobs.db")
    j = _job()
    j.url = "https://boards.example.invalid/jobs/42"
    j.description = "We need someone who writes Python and ships agents."
    mark_seen(conn, j, score=6)

    row = conn.execute(
        "SELECT url, description FROM seen_jobs WHERE source=? AND raw_id=?",
        (j.source, j.raw_id),
    ).fetchone()
    assert row == ("https://boards.example.invalid/jobs/42",
                   "We need someone who writes Python and ships agents.")


def test_connect_migrates_a_legacy_db_in_place(tmp_path):
    """Additive ALTER TABLE, never a recreate: the existing rows survive."""
    db = tmp_path / "jobs.db"
    _legacy_db(db)

    conn = connect(db)

    cols = _columns(conn)
    assert "url" in cols and "description" in cols
    # every original column is still there, in its original order
    assert cols[:9] == ["source", "raw_id", "company", "role", "location",
                        "norm_key", "first_seen", "score", "status"]
    # and the pre-existing row is untouched, with NULLs for the new columns
    assert conn.execute(
        "SELECT company, score, url, description FROM seen_jobs WHERE raw_id='old-1'"
    ).fetchone() == ("Acme", 6, None, None)


def test_connect_migration_is_idempotent(tmp_path):
    """connect() runs on every scan, so the migration must be a no-op the
    second (and third) time -- a bare ALTER TABLE would raise 'duplicate
    column name'."""
    db = tmp_path / "jobs.db"
    _legacy_db(db)

    for _ in range(3):
        conn = connect(db)
        cols = _columns(conn)
        assert cols.count("url") == 1 and cols.count("description") == 1
        conn.close()

    # a fresh DB is also stable across repeated connects
    fresh = tmp_path / "fresh.db"
    for _ in range(3):
        connect(fresh).close()
    assert _columns(connect(fresh)).count("url") == 1


def test_mark_seen_zero_score_is_a_genuine_zero(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    mark_seen(conn, _job(raw_id="z"), score=0)
    assert conn.execute(
        "SELECT score, status FROM seen_jobs WHERE raw_id='z'").fetchone() == (0, "scored")


def test_mark_seen_missing_score_is_null_and_unscored(tmp_path):
    """565 of the live 1,241 rows read score=0 because r.get("score", 0)
    silently turned a response with no score key into a genuine zero. A
    missing score is now NULL + status 'unscored', i.e. re-scorable, and
    never mistaken for the model rating the job a 0."""
    conn = connect(tmp_path / "jobs.db")
    mark_seen(conn, _job(raw_id="n"), score=None)
    assert conn.execute(
        "SELECT score, status FROM seen_jobs WHERE raw_id='n'").fetchone() == (None, "unscored")
    # still deduped like any other seen job
    assert is_new(conn, _job(raw_id="n")) is False


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


def test_his_own_linkedin_applications_do_not_use_scouts_daily_cap(tmp_path):
    """APPLY_DAILY_CAP limits what SCOUT sends. Applications Teodor made by
    hand and ticked on /scout stay in the ledger (duplicate block) but must not
    eat Scout's slots for the day."""
    from cv_tailor.cache import applications_sent_today, connect, record_application
    conn = connect(tmp_path / "jobs.db")
    record_application(conn, job_id="a", company="A", role="R", url="u", channel="portal")
    record_application(conn, job_id="b", company="B", role="R", url="u", channel="linkedin-manual")
    assert applications_sent_today(conn) == 1
