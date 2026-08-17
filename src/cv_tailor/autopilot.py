"""Scout autopilot: the policy layer that replaces the manual approve tap.

Spec: clawd docs/superpowers/specs/2026-07-23-scout-autopilot-design.md.
Runs right after the daily scan (scripts/run_scan.sh -> scripts/autopilot.py).

This is the SOLE owner of approving and applying. scripts/scan.py discovers,
scores and writes the queue, and stops there.

Policy per run:
  1. Approve: every `pending` entry scoring at or above auto_approve_min() in
     any day directory inside the EXPIRE_DAYS window is CAS-approved
     (pending -> approved, approved_by="autopilot") highest score first, and
     handed to scripts/apply_approved.py -- the same orchestrator the /scout
     tap spawns. Anything below the floor stays pending for the manual tap.
  2. Expire: `pending` / `needs_review` / `needs_human` entries whose last
     status change (status_changed_at, else decided_at, else the scan date)
     is older than EXPIRE_DAYS auto-reject with error="auto_expired".
  3. Digest: one Telegram message via notify, only when something happened.

Single-writer discipline is preserved: this module writes ONLY the
pending -> approved/rejected transitions (the sidecar's role); every
post-approval status still belongs to scripts/apply_approved.py, which runs
as a subprocess. APPLY_ARMED / APPLY_DAILY_CAP are enforced there, not here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cv_tailor.scout_queue import StatusConflict, queue_root, update_entry

AUTO_APPROVE_MIN = 6  # hard floor; SCOUT_AUTO_APPROVE_MIN may raise it, never lower it
EXPIRE_DAYS = 7
ORCHESTRATOR_TIMEOUT = 1200  # per-job backstop; portal runs have their own wall clock
EXPIRABLE_STATUSES = ("pending", "needs_review", "needs_human")
# Mid-flight statuses written by scripts/apply_approved.py. A killed
# orchestrator leaves an entry parked in one of these forever: the daily pass
# only looks at `pending`, and _sweep_expired only looks at EXPIRABLE_STATUSES.
STRANDED_STATUSES = ("assembling", "sending")
# Older than this in a mid-flight status = the process that owned it is gone.
# Comfortably longer than ORCHESTRATOR_TIMEOUT so a live run is never stolen.
STRANDED_AFTER = timedelta(seconds=ORCHESTRATOR_TIMEOUT * 3)
_APPLIED = ("sent", "preview_sent")
_PARKED = ("needs_review", "needs_human", "ready")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ROOT = Path(__file__).resolve().parents[2]
_ORCHESTRATOR = _ROOT / "scripts" / "apply_approved.py"
DEFAULT_DB_PATH = _ROOT / "data" / "jobs.db"
SCOUT_URL = "https://admin.teodorlutoiu.com/scout"


def auto_approve_min() -> int:
    """The score an entry must reach before autopilot approves and applies to it
    unattended. SCOUT_AUTO_APPROVE_MIN, read fresh on every call (never cached
    at import time, so tests -- and prod config -- can vary it per-run), same
    contract as portal.base.handoff_timeout_s. Default AUTO_APPROVE_MIN (6).

    Clamped from below at AUTO_APPROVE_MIN: the env var can only RAISE the bar.
    6 is Teodor's stated decision about the lowest score worth a real
    application sent under his own name, so it is not something a .env typo, a
    malformed value, or a future edit reaching for 5 gets to lower. A value
    that will not parse as an int falls back to the floor rather than to
    "no floor"."""
    try:
        wanted = int(os.environ.get("SCOUT_AUTO_APPROVE_MIN", AUTO_APPROVE_MIN))
    except (TypeError, ValueError):
        return AUTO_APPROVE_MIN
    return max(AUTO_APPROVE_MIN, wanted)


@dataclass
class AutopilotReport:
    applied: list = field(default_factory=list)     # (scan_date, entry)
    parked: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    queued_new: list = field(default_factory=list)
    expired: list = field(default_factory=list)
    stranded: list = field(default_factory=list)

    def has_activity(self) -> bool:
        return bool(self.applied or self.parked or self.failed
                    or self.queued_new or self.expired or self.stranded)


def _day_dirs(queue_dir=None) -> list[tuple[str, Path]]:
    root = queue_root(queue_dir)
    if not root.exists():
        return []
    days = [(p.name, p) for p in root.iterdir() if p.is_dir() and _DAY_RE.match(p.name)]
    return sorted(days)


def _read_day(day_dir: Path) -> list[dict]:
    path = day_dir / "jobs.json"
    if not path.exists():
        return []
    try:
        import json
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return []


def _last_change(entry: dict, scan_date: str) -> datetime:
    for key in ("status_changed_at", "decided_at", "applied_at"):
        raw = entry.get(key)
        if raw:
            try:
                ts = datetime.fromisoformat(raw)
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.fromisoformat(scan_date).replace(tzinfo=timezone.utc)


def run_orchestrator(scan_date: str, job_id: str) -> int:
    """Run scripts/apply_approved.py to completion for one approved job.

    Synchronous on purpose: the digest reports final outcomes, and sequential
    runs keep at most one browser/SMTP session alive. Env is inherited
    (run_scan.sh sourced .env; apply_approved also self-bootstraps)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(_ORCHESTRATOR), scan_date, job_id],
            timeout=ORCHESTRATOR_TIMEOUT,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 1


def _approve(scan_date: str, job_id: str, *, queue_dir=None) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()

    def _mut(e: dict) -> None:
        e["status"] = "approved"
        e["decided_at"] = now
        e["approved_by"] = "autopilot"

    try:
        return update_entry(scan_date, job_id, _mut, queue_dir=queue_dir,
                             expect_status="pending")
    except (StatusConflict, KeyError):
        return None


def _reload(scan_date: str, job_id: str, *, queue_dir=None) -> dict:
    for e in _read_day(queue_root(queue_dir) / scan_date):
        if e.get("id") == job_id:
            return e
    return {"id": job_id, "status": "failed", "error": "entry vanished"}


def _sweep_expired(now: datetime, *, queue_dir=None) -> list[tuple[str, dict]]:
    cutoff = now - timedelta(days=EXPIRE_DAYS)
    expired: list[tuple[str, dict]] = []
    for scan_date, day_dir in _day_dirs(queue_dir):
        for entry in _read_day(day_dir):
            status = entry.get("status")
            if status not in EXPIRABLE_STATUSES:
                continue
            if _last_change(entry, scan_date) >= cutoff:
                continue

            def _mut(e: dict) -> None:
                e["status"] = "rejected"
                e["error"] = "auto_expired"
                e["decided_at"] = now.isoformat()

            try:
                fresh = update_entry(scan_date, entry["id"], _mut,
                                      queue_dir=queue_dir, expect_status=status)
                expired.append((scan_date, fresh))
            except (StatusConflict, KeyError):
                continue
    return expired


def ledger_db_path() -> Path:
    """The applications ledger scripts/apply_approved.py writes: SCOUT_DB_PATH
    when set, else <repo>/data/jobs.db -- the same resolution as that script's
    _db_path(). Read per call, never cached at import, so a config change (or
    a test) takes effect without a restart."""
    env = os.environ.get("SCOUT_DB_PATH")
    return Path(env) if env else DEFAULT_DB_PATH


def _ledger_has(job_id: str) -> bool:
    """True iff the applications ledger already owns a row for this job_id.

    Opened lazily and per-call so the sweep never holds a connection, and so
    a missing/locked DB degrades to "unknown" rather than killing the pass.
    Unknown is treated as NOT sent by the caller, which parks the entry for a
    human instead of retrying it -- the fail-closed direction.

    The path is resolved, not omitted: this used to call cache.connect(None),
    whose Path(None) raised TypeError straight into the except below, so the
    function ALWAYS returned False and a stranded `sending` entry could never
    be reconciled to `sent` however clearly the ledger proved the send landed.
    A missing file returns early rather than being opened, because
    cache.connect() CREATES the database it is pointed at -- probing an absent
    ledger must not leave a phantom empty one behind."""
    path = ledger_db_path()
    if not path.exists():
        return False
    try:
        from cv_tailor import cache
        conn = cache.connect(path)
    except Exception:  # noqa: BLE001 - ledger unavailable, caller parks instead
        return False
    try:
        return cache.own_application_recorded(conn, job_id)
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _sweep_stranded(now: datetime, *, queue_dir=None,
                    ledger_has=None) -> list[tuple[str, dict]]:
    """Resolve entries a killed orchestrator left mid-flight.

    Asymmetric by design, because the two mid-flight statuses carry very
    different risk:

    `assembling` -- assembly only renders a CV and a cover letter, nothing has
    left the machine, so the entry goes back to `approved` for a clean retry.

    `sending` -- a send may ALREADY have reached a real employer, and the
    crash could have landed between the send and the ledger write. There is no
    safe automatic retry. If the ledger proves the send landed, reconcile to
    `sent`; otherwise park at `needs_human`. Under no ledger state does a
    stranded send go back to `approved`: applying twice to the same employer
    is worse than applying zero times.
    """
    ledger_has = ledger_has or _ledger_has
    cutoff = now - STRANDED_AFTER
    stranded: list[tuple[str, dict]] = []
    for scan_date, day_dir in _day_dirs(queue_dir):
        for entry in _read_day(day_dir):
            status = entry.get("status")
            if status not in STRANDED_STATUSES:
                continue
            # A fresh mid-flight entry belongs to a process that is probably
            # still working. Never steal it.
            if _last_change(entry, scan_date) >= cutoff:
                continue

            if status == "assembling":
                new_status, err = "approved", "requeued-after-strand"
            elif ledger_has(entry["id"]):
                new_status, err = "sent", "reconciled-after-strand"
            else:
                new_status, err = "needs_human", "stranded-sending-unverified"

            def _mut(e: dict, _s=new_status, _e=err) -> None:
                e["status"] = _s
                e["error"] = _e

            try:
                fresh = update_entry(scan_date, entry["id"], _mut,
                                      queue_dir=queue_dir, expect_status=status)
                stranded.append((scan_date, fresh))
            except (StatusConflict, KeyError):
                continue
    return stranded


def build_digest(report: AutopilotReport) -> str | None:
    if not report.has_activity():
        return None
    lines = [f"Scout autopilot - {datetime.now(timezone.utc).date().isoformat()}"]

    def _section(title: str, rows: list, detail) -> None:
        if not rows:
            return
        lines.append("")
        lines.append(f"{title} ({len(rows)}):")
        for scan_date, e in rows:
            lines.append(f"- {e.get('company')} / {e.get('title')}{detail(e)}")

    _section("Applied", report.applied,
             lambda e: f" [{e.get('apply_method', '?')}, {e.get('status')}]")
    _section("Parked for you", report.parked,
             lambda e: f" ({e.get('status')}: {e.get('error') or 'cover warnings'})")
    _section("Failed", report.failed, lambda e: f": {e.get('error') or '?'}")
    _section("Queued for review", report.queued_new,
             lambda e: f" [{e.get('score')}/10]")
    _section("Auto-expired", report.expired, lambda e: "")
    # Loud, never silent: a stranded send that could not be verified against
    # the ledger is the one row here that wants a human within the hour.
    _section("Recovered after a crash", report.stranded,
             lambda e: f" -> {e.get('status')} ({e.get('error')})")
    lines.append("")
    lines.append(f"Review: {SCOUT_URL}")
    return "\n".join(lines)


def run_autopilot(now: datetime | None = None, *, queue_dir=None,
                  runner=None, notify=None, ledger_has=None) -> AutopilotReport:
    """One full autopilot pass. `runner`/`notify`/`ledger_has` are injectable
    for tests; production is runner=run_orchestrator, notify=telegram.send_text
    and ledger_has=_ledger_has (the real applications table)."""
    now = now or datetime.now(timezone.utc)
    runner = runner or run_orchestrator
    report = AutopilotReport()

    # Resolve anything a killed orchestrator left mid-flight BEFORE starting
    # new work, so stranded entries can never accumulate across days.
    report.stranded = _sweep_stranded(now, queue_dir=queue_dir,
                                      ledger_has=ledger_has)
    window_start = (now - timedelta(days=EXPIRE_DAYS)).date().isoformat()
    today = now.date().isoformat()

    # Resolved once per pass so every candidate in one run is judged by the same
    # floor, and re-resolved on the next run so a config change needs no restart.
    min_score = auto_approve_min()

    candidates: list[tuple[str, dict]] = []
    for scan_date, day_dir in _day_dirs(queue_dir):
        if scan_date < window_start:
            continue
        for entry in _read_day(day_dir):
            if entry.get("status") == "pending" and int(entry.get("score") or 0) >= min_score:
                candidates.append((scan_date, entry))
    candidates.sort(key=lambda pair: int(pair[1].get("score") or 0), reverse=True)

    for scan_date, entry in candidates:
        job_id = entry["id"]
        if _approve(scan_date, job_id, queue_dir=queue_dir) is None:
            continue  # lost the race to a manual tap; its spawn owns the job now
        try:
            runner(scan_date, job_id)
        except Exception:  # noqa: BLE001 - a crashed runner must not kill the pass
            report.failed.append((scan_date, _reload(scan_date, job_id, queue_dir=queue_dir)))
            continue
        final = _reload(scan_date, job_id, queue_dir=queue_dir)
        status = final.get("status")
        if status in _APPLIED:
            report.applied.append((scan_date, final))
        elif status in _PARKED:
            report.parked.append((scan_date, final))
        else:
            report.failed.append((scan_date, final))

    for scan_date, day_dir in _day_dirs(queue_dir):
        if scan_date != today:
            continue
        for entry in _read_day(day_dir):
            if entry.get("status") == "pending":
                report.queued_new.append((scan_date, entry))

    report.expired = _sweep_expired(now, queue_dir=queue_dir)

    text = build_digest(report)
    if text is not None and notify is not None:
        try:
            notify(text)
        except Exception:  # noqa: BLE001 - digest delivery must never fail the run
            pass
    return report
