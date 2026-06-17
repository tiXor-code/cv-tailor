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
    PRIMARY KEY (source, raw_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_norm ON seen_jobs(norm_key);
CREATE TABLE IF NOT EXISTS company_enrichment (
    domain TEXT PRIMARY KEY, is_smb INTEGER, headcount TEXT, signal TEXT, fetched_at TEXT
);
"""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())


def _key(company: str, role: str) -> str:
    return f"{_norm(company)}|{_norm(role)}"


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
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


def mark_seen(conn: sqlite3.Connection, job, score: int, status: str = "scored") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs "
        "(source, raw_id, company, role, location, norm_key, first_seen, score, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (job.source, job.raw_id, job.org, job.title, job.location,
         _key(job.org, job.title),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), score, status),
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
