"""Scout autopilot: the policy layer that replaces the manual approve tap.

Spec: clawd docs/superpowers/specs/2026-07-23-scout-autopilot-design.md.
Runs right after the daily scan (scripts/run_scan.sh -> scripts/autopilot.py).

Policy per run:
  1. Approve: every `pending` entry with score >= AUTO_APPROVE_MIN in any day
     directory inside the EXPIRE_DAYS window is CAS-approved
     (pending -> approved, approved_by="autopilot") highest score first, and
     handed to scripts/apply_approved.py -- the same orchestrator the /scout
     tap spawns. Score-7 entries stay pending for the manual tap.
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

import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cv_tailor.scout_queue import StatusConflict, queue_root, update_entry

AUTO_APPROVE_MIN = 8
EXPIRE_DAYS = 7
ORCHESTRATOR_TIMEOUT = 1200  # per-job backstop; portal runs have their own wall clock
EXPIRABLE_STATUSES = ("pending", "needs_review", "needs_human")
_APPLIED = ("sent", "preview_sent")
_PARKED = ("needs_review", "needs_human", "ready")
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ORCHESTRATOR = Path(__file__).resolve().parents[2] / "scripts" / "apply_approved.py"
SCOUT_URL = "https://admin.teodorlutoiu.com/scout"


@dataclass
class AutopilotReport:
    applied: list = field(default_factory=list)     # (scan_date, entry)
    parked: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    queued_new: list = field(default_factory=list)
    expired: list = field(default_factory=list)

    def has_activity(self) -> bool:
        return bool(self.applied or self.parked or self.failed
                    or self.queued_new or self.expired)


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
    lines.append("")
    lines.append(f"Review: {SCOUT_URL}")
    return "\n".join(lines)


def run_autopilot(now: datetime | None = None, *, queue_dir=None,
                  runner=None, notify=None) -> AutopilotReport:
    """One full autopilot pass. `runner`/`notify` are injectable for tests;
    production is runner=run_orchestrator, notify=telegram.send_text."""
    now = now or datetime.now(timezone.utc)
    runner = runner or run_orchestrator
    report = AutopilotReport()
    window_start = (now - timedelta(days=EXPIRE_DAYS)).date().isoformat()
    today = now.date().isoformat()

    candidates: list[tuple[str, dict]] = []
    for scan_date, day_dir in _day_dirs(queue_dir):
        if scan_date < window_start:
            continue
        for entry in _read_day(day_dir):
            if entry.get("status") == "pending" and int(entry.get("score") or 0) >= AUTO_APPROVE_MIN:
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
