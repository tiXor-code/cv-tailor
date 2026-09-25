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
from cv_tailor.apply_policy import (
    NO_SUBMIT_REASON_PREFIXES,
    NO_SUBMIT_REASONS,
    proves_no_submission,
)
from cv_tailor.ats_resolve import resolve_ats_url
from cv_tailor import linkedin_handoff
from cv_tailor.assemble import AssembleError, assemble_package
from cv_tailor.cache import (
    application_exists,
    applications_sent_today,
    connect,
    delete_application,
    own_application_recorded,
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


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Best-effort .env bootstrap for the detached-spawn path.

    The mac-sidecar spawns this script with launchd's environment, which has
    none of the Azure/SMTP/Telegram keys (scan.py gets them from run_scan.sh
    sourcing .env; there is no wrapper here). Explicit environment always wins
    (setdefault), so tests and shell runs that export their own values are
    untouched."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


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


# Reason prefixes that name the required application question the run died on,
# mapped to the kind of block. Deliberately two kinds and one field: both mean
# "a required question stopped this job", but they need DIFFERENT fixes, so
# collapsing them would poison the count. unanswerable = no grounded answer
# exists, i.e. answers.yaml/profile.yaml is missing something and adding it
# unblocks the job. unwritable = an answer existed and would not land in the
# DOM (readonly field, stale selector, unmatched option), i.e. an adapter bug;
# adding another answer fixes nothing.
_BLOCKED_QUESTION_KINDS = {
    "unanswerable-required": "unanswerable",
    "unwritable-required": "unwritable",
}


def parse_blocked_question(reason) -> tuple[str, str] | None:
    """(label, kind) for a reason that names a blocking required question,
    else None.

    Every adapter already builds "unanswerable-required:<label>" /
    "unwritable-required:<label>" and it lands verbatim in the queue entry's
    `error`. The label is the one fact worth aggregating -- which questions
    actually cost us applications -- and it was only reachable by
    substring-matching an error string.

    Splits on the FIRST colon only: real ATS labels contain colons. The prefix
    must be the whole thing before it, so an exception text that merely
    mentions the phrase is not mistaken for a blocked question."""
    prefix, sep, label = (reason or "").partition(":")
    kind = _BLOCKED_QUESTION_KINDS.get(prefix.strip())
    if not sep or kind is None:
        return None
    # The label is stored exactly as the adapter emitted it, empty string
    # included: the PRESENCE of the field is what says a question blocked us,
    # so an unlabelled field stays countable instead of being invented or
    # dropped.
    return label.strip(), kind


def _record_blocked_question(e: dict, reason) -> None:
    """Promote the blocking question out of `reason` into first-class fields on
    the queue entry. `error` keeps the full reason string -- this adds, it does
    not replace.

    Called by EVERY mutator that writes a portal outcome -- success included,
    where the reason is "" -- so there is one rule, no per-adapter call sites
    to keep in sync (micro1 has a second reason site that bypasses
    screening.py entirely), and the invariant "these keys describe the LATEST
    attempt" holds by construction rather than by remembering to clear.

    When the reason names no question the fields are REMOVED, never left
    stale. Both directions matter and both are real sequences: a second
    attempt dying on a captcha must not still carry the question the first
    could not answer, and -- the one that corrupts the metric -- a job that
    parks on an unanswerable question, gets the answer added and then SUCCEEDS
    must stop reading as blocked. update_entry mutates the entry in place, so
    silence here means the old value survives."""
    parsed = parse_blocked_question(reason)
    if parsed is None:
        e.pop("blocked_question", None)
        e.pop("blocked_question_kind", None)
        return
    e["blocked_question"], e["blocked_question_kind"] = parsed


def _handoff_linkedin(args, entry: dict, meta: dict, profile: dict, answers: dict) -> int:
    """Put one LinkedIn job on Teodor's list at admin /scout, within the daily cap.

    No per-job Telegram: the autopilot digest is the one summary message and
    links to the list (Teodor, 2026-09-24). He applies on LinkedIn, then ticks
    Applied or Not applying (scripts/mark_handoff.py). The answer sheet is
    stored on the entry so the list can show it next to the link.

    Over the cap the job goes back to pending so tomorrow's autopilot -- highest
    score first -- offers it again. A company|role already in the ledger is not
    handed off: that would ask him to apply twice."""
    conn = connect(_db_path())
    if application_exists(conn, job_id=entry.get("id", ""),
                          company=entry.get("company", ""), role=entry.get("title", "")):
        update_entry(args.scan_date, args.job_id, _status_mut("failed", error="duplicate"))
        print("linkedin handoff blocked: duplicate", file=sys.stderr)
        return 1
    if linkedin_handoff.handoffs_on(linkedin_handoff.today()) >= linkedin_handoff.daily_cap():
        update_entry(args.scan_date, args.job_id, _status_mut("pending"))
        print("linkedin handoff: daily cap reached; waits for tomorrow", file=sys.stderr)
        return 0

    update_entry(args.scan_date, args.job_id, _status_mut(
        "handed_off", handed_off_at=datetime.now(timezone.utc).isoformat(),
        handoff_reason="LinkedIn: apply with Easy Apply",
        answer_sheet=linkedin_handoff.answer_sheet(profile, answers)))
    print(f"linkedin handoff: on his list ({entry.get('company')} / {entry.get('title')})",
          file=sys.stderr)
    return 0


def _handoff_reason(reason) -> str | None:
    """Plain-language reason for his list, or None when the outcome is not a
    provable no-submission wall."""
    r = str(reason or "")
    if r.startswith("submit-rejected"):
        return "The application site's spam filter refused Scout's browser"
    if r.startswith(("captcha", "login-required")):
        return "A captcha or login wall stopped Scout"
    if r.startswith(("no-adapter", "missing-apply-target")):
        return "No form Scout can fill on this site"
    if r.startswith("unanswerable-required:"):
        return "Needs your answer: " + r.split(":", 1)[1].strip()
    return None


def _status_mut(status: str, *, error: str | None = None, **extra):
    """Mutator for a status write that carries no portal reason of its own.

    One rule, no exceptions to remember: wherever a status is persisted, the
    blocked question is re-derived. The portal outcome mutators do that by
    calling _record_blocked_question with their reason; everything else --
    the ledger-gate refusals ("duplicate", "daily-cap"), the mid-flight
    "sending" and "assembling" writes, and the whole email track -- has no
    reason to derive from, so the fields are cleared.

    Without this, an entry that parked on an unanswerable required question and
    then hit a ledger refusal (or an SMTP failure) on a later attempt kept the
    old question beside the new, unrelated error -- a stale reading of exactly
    the metric these fields exist to produce."""
    def _mut(e: dict) -> None:
        e["status"] = status
        if error is not None:
            e["error"] = error
        e.update(extra)
        _record_blocked_question(e, "")
    return _mut


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
            _record_blocked_question(e, result.reason)

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
        _record_blocked_question(e, result.reason)

    entry = update_entry(args.scan_date, args.job_id, _needs_human_or_failed)
    send_text(f"{entry.get('company')} / {entry.get('title')}: portal {result.status} ({result.reason})")
    if result.status == "failed":
        print(f"portal dry-run failed: {result.reason}", file=sys.stderr)
        return 1
    return 0


# Reasons that PROVE no submission could have happened -- see the long-form
# rationale at the use site. Anything NOT listed here keeps its pre-inserted
# ledger row, because the attempt may have touched or even submitted the form.
#
# "handoff-manual: no adapter" is deliberately absent: a human was driving a
# real browser at the posting, so a genuine application is entirely possible
# and deleting the row would let a duplicate go out later.
# The judgement itself now lives in cv_tailor.apply_policy, so the expiry
# sweep in cv_tailor.autopilot applies exactly the same rule days later. Two
# copies would drift, and drift here is silent: a row left behind marks a job
# as applied forever. Re-exported under the original names, which the tests
# and the use site below pin.
_NO_SUBMIT_REASONS = NO_SUBMIT_REASONS

# Families that prove the same thing but carry a diagnostic suffix, so exact
# membership cannot match them. Every adapter verifies the resume upload
# BEFORE any field is typed (ashby) or at least before any submit click
# (greenhouse), and aborts there -- so a resume-upload-failed of any shape
# is as certain a "never submitted" as a captcha wall.
#
# Left un-rolled-back, this family is what permanently blocked Flip GmbH
# (2026-09-10), Checkly (09-12) and Sardine (09-16): each kept a ledger row
# for an application that was never sent, and norm_key then blocked every
# same-company|role sibling as a duplicate forever.
_NO_SUBMIT_REASON_PREFIXES = NO_SUBMIT_REASON_PREFIXES


def _proves_no_submission(reason: str) -> bool:
    """True when `reason` PROVES no submission could have happened, so the
    pre-inserted ledger row must be rolled back. Exact members first, then
    the diagnostic-carrying families."""
    return proves_no_submission(reason)


def _handle_portal(args, entry: dict, meta: dict) -> int:
    """Portal apply path (replaces the Phase A stub that parked every portal
    job at `ready` unattempted): unarmed runs a fill-only dry-run for a
    Teodor-reviewable preview; armed (or --handoff, see below) gates on the
    applications ledger (channel "portal", the same daily-cap/duplicate
    policy as email) and then actually drives the browser submit.

    A needs_human outcome always KEEPS whatever ledger row was recorded --
    run_portal_application's own no-confirmation semantics mean the
    submission may have gone through server-side even with no client-side
    confirmation signal, so deleting the row here could let the same job get
    re-submitted later. Only a definite `failed` (never got close to a real
    submit) rolls back a row THIS run inserted, mirroring sender.py's
    SMTP-exception rollback.

    --handoff (args.handoff) is a headed, human-assisted completion of a
    portal application: it runs the ledger-gated submit path REGARDLESS of
    APPLY_ARMED (a human is watching the browser and doing the CAPTCHA +
    submit click themselves, so the armed gate that exists to stop
    *autonomous* submission doesn't apply). Handoff's allowed start statuses
    (see main()) include needs_human/ready, i.e. it is normally COMPLETING a
    prior attempt -- own_application_recorded(job_id) is true when that
    prior attempt already recorded this exact job's ledger row (e.g. an
    earlier armed run that hit needs_human and kept it). In that case this
    run proceeds straight to the browser without another INSERT
    (own_row_skip below): we already own this application, so there is
    nothing to race and nothing to duplicate-block. A pre-existing row for a
    DIFFERENT job_id sharing the same company|role norm_key is still a
    genuine duplicate and blocks exactly as it always has (own_row_skip is
    only true for THIS job_id).
    """
    profile_path = Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml"))
    profile = load_profile(profile_path, strict=True)
    answers = load_answers()
    client = build_azure_client()

    armed = os.environ.get("APPLY_ARMED", "0") == "1"
    handoff = bool(getattr(args, "handoff", False))

    # Aggregator links (remoteOK/WWR listing pages) have no adapter and would
    # die needs_human("no-adapter") before a browser launches. Resolve them to
    # the company's real ATS posting first; on failure the entry is untouched
    # and the run proceeds to the same needs_human it would have hit anyway.
    from cv_tailor.portal.base import adapter_for  # lazy: playwright import
    current_target = (entry.get("apply_target") or entry.get("url") or "").strip()
    if current_target and adapter_for(current_target) is None:
        resolved = resolve_ats_url(entry)
        if resolved:
            entry = update_entry(args.scan_date, args.job_id, lambda e: e.update(
                apply_target=resolved, apply_target_original=current_target))
            print(f"apply_target resolved to ATS: {resolved}", file=sys.stderr)

    # Supervised LinkedIn Easy Apply (Teodor, 2026-09-24: option B, 10 a day).
    # A LinkedIn job that still has no fillable form after the resolver is
    # handed to HIM rather than parked no-adapter. No browser ever opens
    # LinkedIn, and no ledger row is written: only he knows if he submits.
    # Armed only, so dry runs and a paused system never message him.
    live_target = (entry.get("apply_target") or entry.get("url") or "").strip()
    if (armed and not handoff and entry.get("source") == "linkedin"
            and adapter_for(live_target) is None):
        return _handoff_linkedin(args, entry, meta, profile, answers)

    if not armed and not handoff:
        result = run_portal_application(entry, meta, profile, answers, dry_run=True, client=client)
        return _finish_portal_dry_run(args, result)

    conn = connect(_db_path())
    job_id = entry.get("id", "")
    company = entry.get("company", "")
    role = entry.get("title", "")

    own_row_skip = handoff and own_application_recorded(conn, job_id)

    if own_row_skip:
        entry = update_entry(args.scan_date, args.job_id, _status_mut("sending"))
    else:
        if application_exists(conn, job_id=job_id, company=company, role=role):
            update_entry(args.scan_date, args.job_id, _status_mut("failed", error="duplicate"))
            print("portal blocked: duplicate", file=sys.stderr)
            return 1

        cap = int(os.environ.get("APPLY_DAILY_CAP", "10"))
        if applications_sent_today(conn) >= cap:
            update_entry(args.scan_date, args.job_id, _status_mut("failed", error="daily-cap"))
            print("portal blocked: daily-cap", file=sys.stderr)
            return 1

        entry = update_entry(args.scan_date, args.job_id, _status_mut("sending"))

        # Record BEFORE the browser submit attempt -- the same
        # record-then-submit ordering as sender.py's SMTP path. The INSERT
        # (not the pre-check above) is what arbitrates two concurrent
        # portal submits racing this job_id.
        recorded = record_application(
            conn, job_id=job_id, company=company, role=role,
            url=entry.get("url", ""), channel="portal",
        )
        if not recorded:
            update_entry(args.scan_date, args.job_id, _status_mut("failed", error="duplicate"))
            print("portal blocked: duplicate (race)", file=sys.stderr)
            return 1

    result = run_portal_application(
        entry, meta, profile, answers, dry_run=False, client=client,
        handoff=handoff, notify=send_text if handoff else None,
    )

    if result.status == "submitted":
        now = datetime.now(timezone.utc).isoformat()

        def _sent(e: dict) -> None:
            e["status"] = "sent"
            e["applied_at"] = now
            e["evidence_dir"] = result.evidence_dir
            _record_blocked_question(e, result.reason)

        entry = update_entry(args.scan_date, args.job_id, _sent)
        crm_mark_applied(entry.get("company", ""), entry.get("title", ""), entry.get("url", ""))
        send_text(f"Sent: {entry.get('company')} / {entry.get('title')}")
        return 0

    if result.status == "needs_human":
        # Reasons that PROVE no submission could have happened: no-adapter and
        # missing-apply-target return before a browser is even launched -- they
        # are run_portal_application's early guards (the apply_target scheme
        # check and the unattended adapter_for gate), both of which return
        # before it ever enters sync_playwright; captcha / login-required come only from
        # resolve_blocker, whose call sites in every adapter run strictly BEFORE
        # any field is filled; "captcha not solved" means the human never
        # cleared the wall, so the flow never reached the form. Keeping the
        # pre-recorded ledger row for these (a) marks the job as applied
        # forever, (b) blocks every same-company|role sibling as "duplicate",
        # and (c) burns daily-cap slots on non-submissions -- 6 of 10 slots on
        # 2026-07-10 went to walls, not applications. Ambiguous reasons
        # ("timeout", "no-confirmation", "handoff-timeout: not submitted, form
        # left as-is") stay OUT of this set: those may have touched or even
        # submitted the form, so their row keeps the documented semantics.
        # The set itself lives at module scope so the invariant is testable.
        if not own_row_skip and _proves_no_submission(result.reason):
            delete_application(conn, job_id=job_id)

        # Teodor 2026-09-25: a wall that PROVES nothing was sent goes on his
        # apply-yourself list (admin /scout) with everything ready, instead of
        # a park he has to chase. Ambiguous outcomes (timeout, no-confirmation)
        # never do: they may have submitted.
        why = _handoff_reason(result.reason)
        if why:
            # Only a no-adapter job may wait for tomorrow's slot: re-running it
            # submits nothing. A refused/captcha'd job must never re-run, so
            # it lands on the list regardless of the cap.
            retry_safe = str(result.reason).startswith(("no-adapter", "missing-apply-target"))
            if retry_safe and (linkedin_handoff.handoffs_on(linkedin_handoff.today())
                               >= linkedin_handoff.daily_cap()):
                update_entry(args.scan_date, args.job_id, _status_mut("pending"))
                return 0
            update_entry(args.scan_date, args.job_id, _status_mut(
                "handed_off", error=result.reason,
                handed_off_at=datetime.now(timezone.utc).isoformat(),
                handoff_reason=why, evidence_dir=result.evidence_dir,
                answer_sheet=linkedin_handoff.answer_sheet(profile, answers)))
            print(f"portal wall -> his list: {why}", file=sys.stderr)
            return 0

        def _needs_human(e: dict) -> None:
            e["status"] = "needs_human"
            e["error"] = result.reason
            e["evidence_dir"] = result.evidence_dir
            _record_blocked_question(e, result.reason)

        entry = update_entry(args.scan_date, args.job_id, _needs_human)
        send_text(
            f"{entry.get('company')} / {entry.get('title')}: needs human ({result.reason}). "
            f"Apply manually: {entry.get('apply_target') or entry.get('url')}"
        )
        return 0

    # failed: the attempt never got close enough to a real submission for the
    # ledger row to mean anything -- roll it back so the job can be retried.
    # Only roll back a row THIS run inserted: an own_row_skip completion run
    # never inserted anything, and the pre-existing row from the earlier
    # attempt it was completing may still represent a real (ambiguous)
    # submission -- deleting it here would risk a later genuine duplicate.
    if not own_row_skip:
        delete_application(conn, job_id=job_id)

    def _failed(e: dict) -> None:
        e["status"] = "failed"
        e["error"] = result.reason
        e["evidence_dir"] = result.evidence_dir
        # Adapters only ever pair a required-question reason with needs_human,
        # so this is defensive: the rule is "wherever a portal reason is
        # persisted, the question is promoted", with no exceptions to remember.
        _record_blocked_question(e, result.reason)

    entry = update_entry(args.scan_date, args.job_id, _failed)
    print(f"portal submit failed: {result.reason}", file=sys.stderr)
    try:
        send_text(f"{entry.get('company')} / {entry.get('title')}: portal submit failed ({result.reason})")
    except Exception:  # noqa: BLE001 -- Telegram delivery is best-effort here
        pass
    return 1


def main(argv=None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Assemble + route one approved job")
    ap.add_argument("scan_date", help="e.g. 2026-07-10")
    ap.add_argument("job_id", help="the queue entry id")
    ap.add_argument("--force", action="store_true",
                     help="start from needs_review and send anyway, skipping the warnings stop")
    ap.add_argument("--handoff", action="store_true",
                     help="headed browser fill; human solves any captcha and clicks submit; "
                          "runs regardless of APPLY_ARMED (portal jobs only)")
    args = ap.parse_args(argv)

    entry = _load_entry(args.scan_date, args.job_id)

    # --handoff can COMPLETE a prior attempt (needs_human/ready), not just
    # start a fresh one (approved) -- see _handle_portal's own_row_skip for
    # how a pre-existing ledger row from that prior attempt is handled.
    if args.handoff:
        allowed_start = {"needs_human", "ready", "approved"}
    elif args.force:
        allowed_start = {"needs_review"}
    else:
        allowed_start = {"approved"}

    start_status = entry.get("status")
    if start_status not in allowed_start:
        flags = " ".join(f for f, on in (("--force", args.force), ("--handoff", args.handoff)) if on)
        print(
            f"job {args.job_id} has status {start_status!r}, expected one of "
            f"{sorted(allowed_start)!r}{f' ({flags})' if flags else ''}. Not touched.",
            file=sys.stderr,
        )
        return 2

    # Compare-and-swap the FIRST transition: the pre-check above reads the
    # entry OUTSIDE the flock, so two concurrent spawns for the same job can
    # both pass it before either has written anything. expect_status
    # re-checks the status INSIDE the flock right before the write, so only
    # one spawn wins; the loser gets StatusConflict with the entry untouched.
    # It's simply whatever start_status we just validated above (a single
    # value even though --handoff's allowed_start is a 3-way set).
    expect_status = start_status
    try:
        update_entry(
            args.scan_date, args.job_id, _status_mut("assembling"),
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
        update_entry(args.scan_date, args.job_id, _status_mut("failed", error=error))
        print(f"assemble failed: {error}", file=sys.stderr)
        return 1

    def _write_paths(e: dict) -> None:
        e["package_dir"] = meta["package_dir"]
        e["cv_path"] = meta["cv_path"]
        e["cover_letter_path"] = meta["cover_letter_path"]

    entry = update_entry(args.scan_date, args.job_id, _write_paths)

    warnings = meta.get("cover_letter_warnings") or []
    if warnings and not args.force:
        # Teodor's decision, 2026-09-17: APPLY ANYWAY and send him the letter.
        #
        # Parking here was the last hard human gate in the pipeline. Nothing in
        # autopilot ever advanced a needs_review entry -- _sweep_expired could
        # only reject it -- so Cohere (score 8) and Mistral.ai (7) sat in it
        # indefinitely after clearing every other blocker. A gate nothing can
        # open is not review, it is a dead end.
        #
        # The warning is still surfaced, with the letter itself, so he sees
        # exactly what went out and can follow up. Notification after the
        # fact, not a wall in front of the application.
        letter = ""
        try:
            letter = Path(meta["cover_letter_path"]).read_text()
        except (KeyError, TypeError, OSError):
            letter = "(cover letter unreadable)"
        detail = "; ".join(str(w) for w in warnings)[:300]
        send_text(
            f"{entry.get('company')} / {entry.get('title')}: applying DESPITE "
            f"{len(warnings)} cover-letter warning(s): {detail}\n\n{letter[:1500]}"
        )

    apply_method = entry.get("apply_method")

    if apply_method == "portal":
        try:
            return _handle_portal(args, entry, meta)
        except Exception as exc:  # noqa: BLE001 -- portal setup (load_profile
            # strict=True, load_answers, build_azure_client) or any other
            # unexpected exception in the dispatch must land in the queue as
            # `failed`, mirroring the assemble/send guards above. Without this
            # the job wedges at `assembling` forever: no error recorded, no
            # Telegram note, and the detached process just dies silently.
            error = f"{type(exc).__name__}: {exc}"
            update_entry(args.scan_date, args.job_id, _status_mut("failed", error=error))
            print(f"portal handling failed: {error}", file=sys.stderr)
            try:
                send_text(f"{entry.get('company')} / {entry.get('title')}: portal handling failed ({error})")
            except Exception:  # noqa: BLE001 -- Telegram delivery is best-effort here
                pass
            return 1

    # email
    entry = update_entry(args.scan_date, args.job_id, _status_mut("sending"))

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
        update_entry(args.scan_date, args.job_id, _status_mut("failed", error=error))
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

        entry = update_entry(args.scan_date, args.job_id,
                              _status_mut("sent", applied_at=now))
        crm_mark_applied(entry.get("company", ""), entry.get("title", ""), entry.get("url", ""))
        send_text(f"Sent: {entry.get('company')} / {entry.get('title')}")
        send_document(meta["cv_path"], caption=f"{entry.get('company')} / {entry.get('title')}")
        return 0

    if result.status == "preview_sent":
        entry = update_entry(args.scan_date, args.job_id, _status_mut("preview_sent"))
        send_text(f"[PREVIEW] sent to your inbox: {entry.get('company')} / {entry.get('title')}")
        return 0

    # blocked
    entry = update_entry(args.scan_date, args.job_id,
                          _status_mut("failed", error=result.reason))
    print(f"send blocked: {result.reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
