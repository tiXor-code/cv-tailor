"""Write the daily job scan into the shared Scout approval queue.

The queue is the single source of truth read by the mac-sidecar, the
admin.teodorlutoiu.com /scout page, and Mission Control. One file per day:
<root>/<YYYY-MM-DD>/jobs.json. root defaults to ~/clawd/var/scout and can be
overridden with the SCOUT_QUEUE_DIR env var (used by tests and dry runs).
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path

from cv_tailor.apply_detect import detect_apply_channel
from cv_tailor.enrich import company_domain


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
    method, target = detect_apply_channel(
        getattr(job, "description", "") or "", company_domain(job))
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
        "track": item.get("track", "ai"),
        "package_dir": None,
        "cv_path": None,
        "cover_letter_path": None,
        "apply_method": method if method == "email" else "portal",
        "apply_target": target if method == "email" else job.url,
        "status": "pending",
        "decided_at": None,
    }


def _job_description(item) -> str:
    return getattr(item["job"], "description", "") or ""


def _write_atomic(path: Path, text: str) -> None:
    """Write `text` to `path` via a per-writer-unique tmp name + os.replace.

    A fixed tmp name (the old `<path>.tmp`) lets two concurrent writers to the
    SAME path collide: whichever writes second clobbers the first's tmp file,
    and the first's rename-away then raises FileNotFoundError (this is
    exactly what crashed the live e2e run -- see update_entry). A unique name
    per call means two writers never touch the same tmp path, and os.replace
    (unlike os.rename) atomically overwrites an existing destination on every
    platform we run on."""
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}")
    tmp_path.write_text(text)
    os.replace(tmp_path, path)


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
    _write_atomic(out, json.dumps(entries, indent=2))
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


class StatusConflict(Exception):
    """Raised by update_entry when `expect_status` is given and the entry's
    current status (read inside the flock) doesn't match it -- a
    compare-and-swap failure.

    Closes the double-spawn race: a caller's own pre-check (e.g. "is this
    entry approved?") reads the entry OUTSIDE the flock, so two concurrent
    spawns for the same job can both pass that pre-check before either has
    written anything. Passing `expect_status` re-checks the status INSIDE
    the flock immediately before the mutator runs, so only the first writer
    to reach the lock proceeds; every later one raises here with the entry
    left exactly as the winner wrote it."""

    def __init__(self, job_id: str, expected: str, actual: str):
        self.job_id = job_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"job {job_id!r}: expected status {expected!r}, found {actual!r}")


def update_entry(scan_date_iso: str, job_id: str, mutator, *, queue_dir=None,
                  expect_status=None) -> dict:
    """Atomic, cross-process-serialized read-modify-write of one jobs.json entry.

    `mutator(entry_dict)` mutates the matching entry in place (return value
    ignored). Every post-approval writer (scripts/apply_approved.py, the
    scripts/assemble.py CLI) goes through this so jobs.json never has a
    torn/partial write visible to a concurrent reader (the sidecar, the
    admin UI poll), AND so two orchestrator processes updating different job
    ids in the same day file never race each other: the whole read-modify-
    write is held under an exclusive flock on a sibling `.jobs.lock` file
    (fcntl.flock; POSIX-only, matches every environment this runs on), from
    before the read to after the replace. Without this, a live e2e run
    proved two processes can collide on the write step -- one process's
    rename-away throws FileNotFoundError and the other's status update is
    silently lost. The actual file write still goes through `_write_atomic`
    (unique tmp name + os.replace) so a concurrent reader never sees a torn
    write even outside this lock's scope (e.g. a reader with no lock of its
    own, like the sidecar/admin UI poll).

    `expect_status`, when given, makes this call a compare-and-swap: if the
    entry's current status (checked inside the flock, before the mutator
    runs) does not equal `expect_status`, raises StatusConflict and leaves
    the entry untouched. Used by the orchestrator's first status transition
    so two concurrent spawns for the same job can't both proceed.

    Raises KeyError(job_id) if no entry with that id exists in the day's
    queue -- the caller decides how to report that (print + exit 2 for the
    orchestrator).
    """
    day_dir = queue_root(queue_dir) / scan_date_iso
    path = day_dir / "jobs.json"
    lock_path = day_dir / ".jobs.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            entries = json.loads(path.read_text())
            entry = next((e for e in entries if e.get("id") == job_id), None)
            if entry is None:
                raise KeyError(job_id)

            if expect_status is not None and entry.get("status") != expect_status:
                raise StatusConflict(job_id, expect_status, entry.get("status"))

            mutator(entry)

            _write_atomic(path, json.dumps(entries, indent=2, ensure_ascii=False))
            return entry
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
