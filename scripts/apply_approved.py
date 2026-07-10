#!/usr/bin/env python3
"""Scout Phase A orchestrator -- the ONLY writer of post-approval queue statuses.

Spawned detached by mac-sidecar's POST /admin/scout/decide right after Teodor
approves a job in the /scout UI: `apply_approved.py <scan-date> <job-id>`.
Single-writer discipline: the sidecar writes only the pending->approved /
pending->rejected transition; every status after that belongs to this script.

Flow (exact status vocabulary, atomic update_entry writes throughout):
  approved -> assembling -> (failed | needs_review | ready | sending)
    email:  sending -> (sent | preview_sent | failed)
    portal: unarmed -> a dry-run fill/screenshot only, no ledger touched:
              ready (filled) | needs_human(reason) | failed(reason)
            armed -> ledger gates (duplicate/cap) -> record-then-submit,
              exactly like email's SMTP send:
              sending -> sent (submitted) | needs_human(reason) | failed(reason)
              A needs_human outcome KEEPS the ledger row (the submission may
              have gone through -- see run_portal_application's
              no-confirmation semantics); only a definite failed rolls it back.
--force allows starting from needs_review (the UI's "send anyway") and skips
the cover-letter-warnings stop.

Exit codes: 0 on any terminal success state (sent/preview_sent/ready/
needs_review/needs_human), 1 on failed, 2 on the wrong start status (entry
untouched).

Usage:
  python scripts/apply_approved.py <scan-date> <job-id> [--force]

Env:
  SCOUT_QUEUE_DIR   override the queue root (used by tests; never touches prod state)
  SCOUT_DB_PATH     override the applications-ledger sqlite path (default data/jobs.db)
  CV_TAILOR_PROFILE / CV_TAILOR_TEMPLATES   override profile.yaml / templates dir
  APPLY_ARMED       "1" submits for real (email SMTP send / portal browser submit);
                     anything else previews/dry-runs only, same flag for both channels
  APPLY_DAILY_CAP   max applications/day across BOTH channels while armed (default 10)
Run under the cv-tailor venv; system python3 lacks deps.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.answers import load_answers
from cv_tailor.assemble import AssembleError, assemble_package
from cv_tailor.cache import (
    application_exists,
    applications_sent_today,
    connect,
    delete_application,
    record_application,
)
from cv_tailor.portal import run_portal_application
from cv_tailor.profile import load_profile
from cv_tailor.scout_queue import StatusConflict, queue_root, update_entry
from cv_tailor.sender import send_application
from cv_tailor.sheets import crm_mark_applied
from cv_tailor.tailor_llm import build_azure_client
from cv_tailor.telegram import send_document, send_text

DEFAULT_DB_PATH = ROOT / "data" / "jobs.db"


def _db_path() -> Path:
    env = os.environ.get("SCOUT_DB_PATH")
    return Path(env) if env else DEFAULT_DB_PATH


def _load_entry(scan_date: str, job_id: str, *, queue_dir=None) -> dict:
    path = queue_root(queue_dir) / scan_date / "jobs.json"
    if not path.exists():
        sys.exit(f"queue not found: {path}")
    entries = json.loads(path.read_text())
    for e in entries:
        if e.get("id") == job_id:
            return e
    ids = ", ".join(e.get("id", "?") for e in entries) or "(empty)"
    sys.exit(f"job id {job_id!r} not in queue. Available: {ids}")


def _finish_portal_dry_run(args, result) -> int:
    """Unarmed portal result -> queue status. `filled` means the screening
    honesty guard cleared every required question and the form actually
    took every write -- `ready` keeps its Phase A meaning (a human can apply
    via the link), now backed by a filled-form screenshot. needs_human/failed
    both land on that exact status with the reason + evidence attached."""
    if result.status == "filled":
        def _ready(e: dict) -> None:
            e["status"] = "ready"
            e["evidence_dir"] = result.evidence_dir

        entry = update_entry(args.scan_date, args.job_id, _ready)
        send_text(
            f"{entry.get('company')} / {entry.get('title')}: filled preview staged. "
            f"Apply: {entry.get('apply_target') or entry.get('url')}"
        )
        return 0

    def _needs_human_or_failed(e: dict) -> None:
        e["status"] = result.status
        e["error"] = result.reason
        e["evidence_dir"] = result.evidence_dir

    entry = update_entry(args.scan_date, args.job_id, _needs_human_or_failed)
    send_text(f"{entry.get('company')} / {entry.get('title')}: portal {result.status} ({result.reason})")
    if result.status == "failed":
        print(f"portal dry-run failed: {result.reason}", file=sys.stderr)
        return 1
    return 0


def _handle_portal(args, entry: dict, meta: dict) -> int:
    """Portal apply path (replaces the Phase A stub that parked every portal
    job at `ready` unattempted): unarmed runs a fill-only dry-run for a
    Teodor-reviewable preview; armed gates on the applications ledger
    (channel "portal", the same daily-cap/duplicate policy as email) and
    then actually drives the browser submit.

    A needs_human outcome always KEEPS whatever ledger row was recorded --
    run_portal_application's own no-confirmation semantics mean the
    submission may have gone through server-side even with no client-side
    confirmation signal, so deleting the row here could let the same job get
    re-submitted later. Only a definite `failed` (never got close to a real
    submit) rolls the row back, mirroring sender.py's SMTP-exception rollback.
    """
    profile_path = Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml"))
    profile = load_profile(profile_path, strict=True)
    answers = load_answers()
    client = build_azure_client()

    armed = os.environ.get("APPLY_ARMED", "0") == "1"

    if not armed:
        result = run_portal_application(entry, meta, profile, answers, dry_run=True, client=client)
        return _finish_portal_dry_run(args, result)

    conn = connect(_db_path())
    job_id = entry.get("id", "")
    company = entry.get("company", "")
    role = entry.get("title", "")

    if application_exists(conn, job_id=job_id, company=company, role=role):
        update_entry(args.scan_date, args.job_id, lambda e: e.update(status="failed", error="duplicate"))
        print("portal blocked: duplicate", file=sys.stderr)
        return 1

    cap = int(os.environ.get("APPLY_DAILY_CAP", "10"))
    if applications_sent_today(conn) >= cap:
        update_entry(args.scan_date, args.job_id, lambda e: e.update(status="failed", error="daily-cap"))
        print("portal blocked: daily-cap", file=sys.stderr)
        return 1

    entry = update_entry(args.scan_date, args.job_id, lambda e: e.update(status="sending"))

    # Record BEFORE the browser submit attempt -- the same record-then-submit
    # ordering as sender.py's SMTP path. The INSERT (not the pre-check above)
    # is what arbitrates two concurrent portal submits racing this job_id.
    recorded = record_application(
        conn, job_id=job_id, company=company, role=role,
        url=entry.get("url", ""), channel="portal",
    )
    if not recorded:
        update_entry(args.scan_date, args.job_id, lambda e: e.update(status="failed", error="duplicate"))
        print("portal blocked: duplicate (race)", file=sys.stderr)
        return 1

    result = run_portal_application(entry, meta, profile, answers, dry_run=False, client=client)

    if result.status == "submitted":
        now = datetime.now(timezone.utc).isoformat()

        def _sent(e: dict) -> None:
            e["status"] = "sent"
            e["applied_at"] = now
            e["evidence_dir"] = result.evidence_dir

        entry = update_entry(args.scan_date, args.job_id, _sent)
        crm_mark_applied(entry.get("company", ""), entry.get("title", ""), entry.get("url", ""))
        send_text(f"Sent: {entry.get('company')} / {entry.get('title')}")
        return 0

    if result.status == "needs_human":
        def _needs_human(e: dict) -> None:
            e["status"] = "needs_human"
            e["error"] = result.reason
            e["evidence_dir"] = result.evidence_dir

        entry = update_entry(args.scan_date, args.job_id, _needs_human)
        send_text(
            f"{entry.get('company')} / {entry.get('title')}: needs human ({result.reason}). "
            f"Apply manually: {entry.get('apply_target') or entry.get('url')}"
        )
        return 0

    # failed: the attempt never got close enough to a real submission for the
    # ledger row to mean anything -- roll it back so the job can be retried.
    delete_application(conn, job_id=job_id)
    entry = update_entry(
        args.scan_date, args.job_id,
        lambda e: e.update(status="failed", error=result.reason, evidence_dir=result.evidence_dir),
    )
    print(f"portal submit failed: {result.reason}", file=sys.stderr)
    try:
        send_text(f"{entry.get('company')} / {entry.get('title')}: portal submit failed ({result.reason})")
    except Exception:  # noqa: BLE001 -- Telegram delivery is best-effort here
        pass
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble + route one approved job")
    ap.add_argument("scan_date", help="e.g. 2026-07-10")
    ap.add_argument("job_id", help="the queue entry id")
    ap.add_argument("--force", action="store_true",
                     help="start from needs_review and send anyway, skipping the warnings stop")
    args = ap.parse_args(argv)

    entry = _load_entry(args.scan_date, args.job_id)

    allowed_start = {"needs_review"} if args.force else {"approved"}
    start_status = entry.get("status")
    if start_status not in allowed_start:
        print(
            f"job {args.job_id} has status {start_status!r}, expected one of "
            f"{sorted(allowed_start)!r} ({'with' if args.force else 'without'} --force). "
            f"Not touched.",
            file=sys.stderr,
        )
        return 2

    # Compare-and-swap the FIRST transition: the pre-check above reads the
    # entry OUTSIDE the flock, so two concurrent spawns for the same job can
    # both pass it before either has written anything. expect_status
    # re-checks the status INSIDE the flock right before the write, so only
    # one spawn wins; the loser gets StatusConflict with the entry untouched.
    expect_status = "needs_review" if args.force else "approved"
    try:
        update_entry(
            args.scan_date, args.job_id, lambda e: e.update(status="assembling"),
            expect_status=expect_status,
        )
    except StatusConflict as exc:
        print(f"status conflict, another spawn already claimed this job: {exc}", file=sys.stderr)
        return 2

    try:
        meta = assemble_package(entry, args.scan_date)
    except Exception as exc:  # noqa: BLE001 -- AssembleError or any other assembly
        # failure must land in the queue as `failed`, never crash the orchestrator silently.
        error = str(exc) if isinstance(exc, AssembleError) else f"{type(exc).__name__}: {exc}"
        update_entry(args.scan_date, args.job_id, lambda e: e.update(status="failed", error=error))
        print(f"assemble failed: {error}", file=sys.stderr)
        return 1

    def _write_paths(e: dict) -> None:
        e["package_dir"] = meta["package_dir"]
        e["cv_path"] = meta["cv_path"]
        e["cover_letter_path"] = meta["cover_letter_path"]

    entry = update_entry(args.scan_date, args.job_id, _write_paths)

    warnings = meta.get("cover_letter_warnings") or []
    if warnings and not args.force:
        def _needs_review(e: dict) -> None:
            e["status"] = "needs_review"
            e["warnings"] = warnings

        entry = update_entry(args.scan_date, args.job_id, _needs_review)
        send_text(
            f"{entry.get('company')} / {entry.get('title')}: cover letter needs review "
            f"({len(warnings)} warning(s)). Open /scout to send anyway."
        )
        return 0

    apply_method = entry.get("apply_method")

    if apply_method == "portal":
        return _handle_portal(args, entry, meta)

    # email
    entry = update_entry(args.scan_date, args.job_id, lambda e: e.update(status="sending"))

    profile_path = Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml"))
    profile = load_profile(profile_path, strict=True)
    conn = connect(_db_path())
    pkg_dir = Path(meta["package_dir"])
    try:
        result = send_application(entry, pkg_dir, profile, conn=conn)
    except Exception as exc:  # noqa: BLE001 -- an SMTP/network failure must land
        # in the queue as `failed`, mirroring the assemble failure path above.
        # Without this the job wedges at `sending` forever: no error recorded,
        # no Telegram note, and the detached process just dies silently.
        error = f"{type(exc).__name__}: {exc}"
        update_entry(args.scan_date, args.job_id, lambda e: e.update(status="failed", error=error))
        print(f"send failed: {error}", file=sys.stderr)
        try:
            send_text(
                f"{entry.get('company')} / {entry.get('title')}: send failed ({error})"
            )
        except Exception:  # noqa: BLE001 -- Telegram delivery is best-effort here
            pass
        return 1

    if result.status == "sent":
        now = datetime.now(timezone.utc).isoformat()

        def _sent(e: dict) -> None:
            e["status"] = "sent"
            e["applied_at"] = now

        entry = update_entry(args.scan_date, args.job_id, _sent)
        crm_mark_applied(entry.get("company", ""), entry.get("title", ""), entry.get("url", ""))
        send_text(f"Sent: {entry.get('company')} / {entry.get('title')}")
        send_document(meta["cv_path"], caption=f"{entry.get('company')} / {entry.get('title')}")
        return 0

    if result.status == "preview_sent":
        entry = update_entry(args.scan_date, args.job_id, lambda e: e.update(status="preview_sent"))
        send_text(f"[PREVIEW] sent to your inbox: {entry.get('company')} / {entry.get('title')}")
        return 0

    # blocked
    entry = update_entry(args.scan_date, args.job_id,
                          lambda e: e.update(status="failed", error=result.reason))
    print(f"send blocked: {result.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
