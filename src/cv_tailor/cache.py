# src/cv_tailor/cache.py
"""SQLite cache: cross-source dedup (seen_jobs) + enrichment cache (Phase 2)."""
from __future__ import annotations
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    source TEXT, raw_id TEXT, company TEXT, role TEXT, location TEXT,
    norm_key TEXT, first_seen TEXT, score INTEGER, status TEXT,
    url TEXT, description TEXT,
    PRIMARY KEY (source, raw_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_norm ON seen_jobs(norm_key);
CREATE TABLE IF NOT EXISTS company_enrichment (
    domain TEXT PRIMARY KEY, is_smb INTEGER, headcount TEXT, signal TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY, company TEXT, role TEXT, norm_key TEXT,
    url TEXT, channel TEXT, sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_applications_norm ON applications(norm_key);
"""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())


def norm_pair(company: str, role: str) -> str:
    return f"{_norm(company)}|{_norm(role)}"


def _key(company: str, role: str) -> str:
    return norm_pair(company, role)


# Columns added to seen_jobs after the original schema shipped. A DB created
# from _SCHEMA above already has them; a DB created before they existed (the
# live data/jobs.db, 1,241 rows on 2026-08-17) gains them in place. Recreating
# the table would throw that scan history away, and the history is the point.
_SEEN_JOBS_ADDED_COLUMNS = (
    ("url", "TEXT"),
    ("description", "TEXT"),
)
_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

# score is NULL for a job whose scoring response carried no score at all. Kept
# distinct from a genuine 0 (the model's verdict) so the two are never
# conflated -- see mark_seen.
UNSCORED = "unscored"
# Stored description length. match.score_job only ever reads the first 6,000
# characters, so this keeps a re-score byte-identical while refusing to let one
# pathological posting bloat the DB. Only gate survivors are stored (~11/day),
# not the ~1,200 fetched.
DESCRIPTION_CAP = 20_000


def _migrate_seen_jobs(conn: sqlite3.Connection) -> None:
    """Add any missing _SEEN_JOBS_ADDED_COLUMNS. Idempotent: the PRAGMA guard
    means a second run adds nothing (a bare ALTER TABLE would raise "duplicate
    column name" and take the whole scan down). Additive only -- no data is
    rewritten, moved or dropped.

    The column name is interpolated because SQLite cannot parameterize DDL
    identifiers; it is a module-level literal, never caller input, and the
    identifier check below fails loudly if that ever stops being true."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    for name, decl in _SEEN_JOBS_ADDED_COLUMNS:
        if not (_IDENTIFIER_RE.match(name) and _IDENTIFIER_RE.match(decl.lower())):
            raise ValueError(f"refusing to ALTER with unsafe column spec: {name} {decl}")
        if name not in have:
            conn.execute(f"ALTER TABLE seen_jobs ADD COLUMN {name} {decl}")
    conn.commit()


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    _migrate_seen_jobs(conn)
    return conn


def is_new(conn: sqlite3.Connection, job) -> bool:
    if conn.execute(
        "SELECT 1 FROM seen_jobs WHERE source=? AND raw_id=?",
        (job.source, job.raw_id),
    ).fetchone():
        return False
    if conn.execute(
        "SELECT 1 FROM seen_jobs WHERE norm_key=?", (_key(job.org, job.title),)
    ).fetchone():
        return False
    return True


def mark_seen(conn: sqlite3.Connection, job, score: int | None,
              status: str | None = None) -> None:
    """Record a scored job so it is deduped out of later scans -- and so the
    score can be revisited.

    url + description are stored because they are what a re-score needs. Every
    threshold decision so far has been made by squinting at titles: the 18
    score-6 rows in the live DB could not be re-scored at a different floor
    because the posting text they were scored from was never kept. The full
    JobPosting is in scope at the one call site (scripts/scan.py), so this
    costs nothing but the bytes.

    score=None means the scoring response carried no score at all. It is
    stored as NULL with status UNSCORED, never as a 0: a 0 is the model's
    verdict and an absent score is a broken response, and the old
    r.get("score", 0) made those indistinguishable (565 of the live 1,241 rows
    read 0). Rows written before this change stay ambiguous -- nothing can
    recover which of those 565 were real zeros."""
    if status is None:
        status = "scored" if score is not None else UNSCORED
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs "
        "(source, raw_id, company, role, location, norm_key, first_seen, score, status, "
        "url, description) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (job.source, job.raw_id, job.org, job.title, job.location,
         _key(job.org, job.title),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), score, status,
         getattr(job, "url", "") or "",
         (getattr(job, "description", "") or "")[:DESCRIPTION_CAP]),
    )
    conn.commit()


def put_enrichment(conn, domain, is_smb, headcount, signal):
    conn.execute(
        "INSERT OR REPLACE INTO company_enrichment "
        "(domain, is_smb, headcount, signal, fetched_at) VALUES (?,?,?,?,?)",
        (domain.lower(), 1 if is_smb else 0, headcount, signal,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.commit()


def get_enrichment(conn, domain, max_age_days=30):
    row = conn.execute(
        "SELECT is_smb, headcount, signal, fetched_at FROM company_enrichment WHERE domain=?",
        (domain.lower(),),
    ).fetchone()
    if not row:
        return None
    try:
        fetched = datetime.strptime(row[3], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    age_days = (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
    if age_days > max_age_days:
        return None
    return {"is_smb": bool(row[0]), "headcount": row[1], "signal": row[2], "fetched_at": row[3]}


def record_application(conn: sqlite3.Connection, *, job_id: str, company: str, role: str,
                        url: str, channel: str) -> bool:
    """Insert the ledger row for a sent/sending application.

    Plain INSERT (not INSERT OR REPLACE): the PRIMARY KEY(job_id) constraint
    is what arbitrates a same-job_id race between two concurrent senders --
    the loser's insert raises IntegrityError, caught here and reported as
    False so the caller can block instead of double-sending. Returns True on
    a successful insert."""
    try:
        conn.execute(
            "INSERT INTO applications "
            "(job_id, company, role, norm_key, url, channel, sent_at) VALUES (?,?,?,?,?,?,?)",
            (job_id, company, role, norm_pair(company, role), url, channel,
             datetime.now().strftime("%Y-%m-%dT%H:%M:%S")),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def delete_application(conn: sqlite3.Connection, *, job_id: str) -> None:
    """Roll back a ledger row recorded before an SMTP send that then failed,
    so the job can be retried without tripping the duplicate gate forever."""
    conn.execute("DELETE FROM applications WHERE job_id=?", (job_id,))
    conn.commit()


def own_application_recorded(conn: sqlite3.Connection, job_id: str) -> bool:
    """True iff a ledger row already exists for THIS exact job_id.

    Distinguishes "we already own an application for this job" (a prior
    armed attempt landed at needs_human and kept its row, or a --handoff
    completion run is being retried) from application_exists's broader
    "some job -- possibly a different one -- already claimed this
    company|role norm_key". Used by the armed/handoff record step: an own
    row means proceed straight to the browser without another INSERT
    (we're completing our own prior attempt, not racing a duplicate)."""
    return conn.execute(
        "SELECT 1 FROM applications WHERE job_id=?", (job_id,)
    ).fetchone() is not None


def application_exists(conn: sqlite3.Connection, *, job_id: str, company: str, role: str) -> bool:
    if conn.execute("SELECT 1 FROM applications WHERE job_id=?", (job_id,)).fetchone():
        return True
    if conn.execute(
        "SELECT 1 FROM applications WHERE norm_key=?", (norm_pair(company, role),)
    ).fetchone():
        return True
    return False


# Ledger channel for a LinkedIn application Teodor made himself and ticked on
# admin /scout (scripts/mark_handoff.py). Kept in the ledger so the duplicate
# block covers it, but never counted against Scout's own daily cap.
MANUAL_CHANNEL = "linkedin-manual"


def applications_sent_today(conn: sqlite3.Connection) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE substr(sent_at, 1, 10) = ? AND channel != ?",
        (today, MANUAL_CHANNEL),
    ).fetchone()
    return row[0] if row else 0
