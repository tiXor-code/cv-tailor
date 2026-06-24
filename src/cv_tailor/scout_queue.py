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


def write_jobs_queue(scored, scan_date, *, queue_dir=None) -> Path:
    """Write the day's scored jobs to <root>/<date>/jobs.json. Returns the path."""
    day_dir = queue_root(queue_dir) / scan_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    out = day_dir / "jobs.json"
    out.write_text(json.dumps([_to_entry(it) for it in scored], indent=2))
    return out
