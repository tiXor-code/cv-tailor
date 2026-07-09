"""Write the daily job scan into the shared Scout approval queue.

The queue is the single source of truth read by the mac-sidecar, the
admin.teodorlutoiu.com /scout page, and Mission Control. One file per day:
<root>/<YYYY-MM-DD>/jobs.json. root defaults to ~/clawd/var/scout and can be
overridden with the SCOUT_QUEUE_DIR env var (used by tests and dry runs).
"""
import hashlib
import json
import os
from pathlib import Path


def queue_root(queue_dir=None) -> Path:
    if queue_dir is not None:
        return Path(queue_dir)
    env = os.environ.get("SCOUT_QUEUE_DIR")
    return Path(env) if env else Path.home() / "clawd" / "var" / "scout"


def _job_id(job) -> str:
    """Stable id from source + the source's raw id (falls back to url)."""
    raw = getattr(job, "raw_id", "") or getattr(job, "url", "")
    basis = f"{getattr(job, 'source', '')}:{raw}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _to_entry(item) -> dict:
    job = item["job"]
    return {
        "id": _job_id(job),
        "source": job.source,
        "title": job.title,
        "company": job.org,
        "location": job.location,
        "url": job.url,
        "score": int(item["score"]),
        "why": item.get("reason", ""),
        "matched": list(item.get("keywords", []) or []),
        "package_dir": None,
        "cv_path": None,
        "cover_letter_path": None,
        "apply_method": "portal",
        "apply_target": job.url,
        "status": "pending",
        "decided_at": None,
    }


def _job_description(item) -> str:
    return getattr(item["job"], "description", "") or ""


def write_jobs_queue(scored, scan_date, *, queue_dir=None) -> Path:
    """Write the day's scored jobs to <root>/<date>/jobs.json. Returns the path.

    Also writes a sibling descriptions.json (id -> full JD text). The JD is kept
    OUT of jobs.json so the queue the UI/sidecar reads stays lean, but the
    approve-to-assemble step (scripts/assemble.py) needs the full text, so it is
    persisted here at scan time."""
    day_dir = queue_root(queue_dir) / scan_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    entries = [_to_entry(it) for it in scored]
    out = day_dir / "jobs.json"
    out.write_text(json.dumps(entries, indent=2))
    descriptions = {e["id"]: _job_description(it) for e, it in zip(entries, scored)}
    (day_dir / "descriptions.json").write_text(
        json.dumps(descriptions, indent=2, ensure_ascii=False))
    return out


def read_description(scan_date_iso: str, job_id: str, *, queue_dir=None) -> str:
    """Full JD text for a queued job, from the descriptions.json sidecar. Empty
    string if the sidecar is missing (older scans predate it) or the id is absent."""
    p = queue_root(queue_dir) / scan_date_iso / "descriptions.json"
    if not p.exists():
        return ""
    try:
        return (json.loads(p.read_text()).get(job_id) or "").strip()
    except (json.JSONDecodeError, OSError):
        return ""
