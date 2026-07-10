# scripts/scan.py
#!/usr/bin/env python3
"""Daily job-discovery scanner (v2 funnel).

Pipeline: fetch -> Gate 1 (rules) -> Gate 2 (SMB provenance) -> Gate 3 (dedup vs
SQLite + CRM) -> LLM score survivors -> write digest -> quiet Telegram (only when
new qualifying roles exist). Scoring/tailoring/CRM unchanged.

Usage: python scripts/scan.py [--min-score 7] [--max-results 10] [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from cv_tailor.profile import load_profile
from cv_tailor.tailor_llm import build_azure_client
from cv_tailor.job_sources import fetch_all
from cv_tailor.match import score_job
from cv_tailor.digest import format_digest
from cv_tailor.telegram import format_digest_for_telegram, send_text
from cv_tailor.scout_queue import queue_root, update_entry, write_jobs_queue
from cv_tailor.budget import SerpBudget


def _is_auth_error(exc: Exception) -> bool:
    """True for a 401/invalid-key failure. Checked by shape, not by importing the
    openai SDK's exception classes, so it survives SDK changes."""
    if exc.__class__.__name__ in ("AuthenticationError", "PermissionDeniedError"):
        return True
    if getattr(exc, "status_code", None) == 401:
        return True
    blob = str(exc).lower()
    return "401" in blob and ("invalid subscription key" in blob
                             or "access denied" in blob
                             or "incorrect api key" in blob)
from cv_tailor.cache import connect, is_new, mark_seen
from cv_tailor.gates import passes_gate1_tracks
from cv_tailor.enrich import is_smb, smb_hint

DB_PATH = ROOT / "data" / "jobs.db"
BUDGET_PATH = ROOT / "data" / "serpapi_budget.json"


def run_gates(jobs, tracks, conn):
    """Gate 1 (track-aware rules) -> Gate 2 (SMB) -> Gate 3 (dedup). Each
    survivor gains a `.track` attribute set to its winning track id (see
    gates.passes_gate1_tracks). Returns survivors."""
    survivors = []
    for j in jobs:
        track = passes_gate1_tracks(j, tracks)
        if track is None:
            continue
        j.track = track
        if not is_smb(j, conn):
            continue
        if not is_new(conn, j):
            continue
        survivors.append(j)
    return survivors


def should_send(scored):
    return len(scored) > 0


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())


def drop_crm_tracked(jobs, tracked_keys):
    """Gate 3's CRM half (SQLite is the other). Drop jobs whose (company, role)
    already appears in the Sheets CRM. tracked_keys is a set of
    (norm_company, norm_role) tuples."""
    return [j for j in jobs if (_norm(j.org), _norm(j.title)) not in tracked_keys]


def crm_tracked_keys():
    """Read (norm_company, norm_role) pairs already tracked in the Sheets CRM.
    Returns an empty set on any failure so the scan degrades to SQLite-only dedup."""
    try:
        from cv_tailor.sheets import get_pipeline_worksheet
        rows = get_pipeline_worksheet().get_all_values()
    except Exception as e:
        print(f"warning: CRM dedup unavailable ({e}); SQLite-only", file=sys.stderr)
        return set()
    keys = set()
    for i, row in enumerate(rows):
        if i == 0 or len(row) < 2:
            continue
        keys.add((_norm(row[0]), _norm(row[1])))
    return keys


def _auto_apply_enabled(*, dry_run: bool) -> bool:
    """Auto-apply is armed only on a real run (never --dry-run) with
    AUTO_APPLY=1 in the environment. Default is off ("0")."""
    return not dry_run and os.environ.get("AUTO_APPLY", "0") == "1"


def _default_apply_runner(scan_date_iso: str, entry_id: str, log_path: Path) -> int:
    """Spawn scripts/apply_approved.py for one job and wait for it (<=300s).
    stdout/stderr are appended to log_path. The orchestrator itself owns the
    armed/cap/dedupe gates -- this only runs it and reports the exit code."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as log_f:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "apply_approved.py"), scan_date_iso, entry_id],
            timeout=300, cwd=ROOT, stdout=log_f, stderr=subprocess.STDOUT,
        )
    return proc.returncode


def auto_apply_pending(scan_date_iso: str, *, queue_dir=None, runner=None) -> list[tuple[str, int]]:
    """Auto-approve and sequentially run the orchestrator for every 'pending'
    job in the day's queue, in queue order (already best-first -- scored jobs
    are score-sorted before write_jobs_queue writes them).

    For each pending entry: mark it "approved" with decided_at (UTC iso) and
    decided_by "auto" via scout_queue.update_entry, then run `runner(
    scan_date_iso, entry_id, log_path) -> returncode` (default:
    _default_apply_runner, a synchronous, waited-on subprocess.run of
    scripts/apply_approved.py). A failing or timing-out runner does NOT stop
    the loop -- any exception from the runner call is caught and recorded as
    rc=-1, and the next pending job still gets a shot.

    Non-pending entries are left untouched. Returns the (job_id, returncode)
    list in the order jobs were run."""
    run = runner or _default_apply_runner
    day_dir = queue_root(queue_dir) / scan_date_iso
    entries = json.loads((day_dir / "jobs.json").read_text())

    results: list[tuple[str, int]] = []
    for entry in entries:
        if entry.get("status") != "pending":
            continue
        job_id = entry["id"]
        now = datetime.now(timezone.utc).isoformat()

        def _approve(e: dict, _now=now) -> None:
            e["status"] = "approved"
            e["decided_at"] = _now
            e["decided_by"] = "auto"

        update_entry(scan_date_iso, job_id, _approve, queue_dir=queue_dir)

        log_path = day_dir / "logs" / f"{job_id}.log"
        try:
            rc = run(scan_date_iso, job_id, log_path)
        except Exception as exc:
            rc = -1
            print(f"auto-apply: {job_id} runner error: {exc}", file=sys.stderr)

        print(f"auto-apply: {job_id} {entry.get('company', '?')} -> rc={rc}", file=sys.stderr)
        results.append((job_id, rc))

    ok = sum(1 for _, rc in results if rc == 0)
    print(f"auto-apply summary: {ok}/{len(results)} succeeded", file=sys.stderr)
    return results


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--min-score", type=int, default=7)
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--dry-run", action="store_true", help="No Telegram, throwaway DB.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = load_profile("profile.yaml")
    # tracks: {} config drives Gate 1 track tagging. Falls back to a single
    # 'ai' track built from the legacy target_keywords list so an older
    # profile.yaml (missing the tracks: block) doesn't crash the scan.
    tracks = profile.get("tracks") or {"ai": {"keywords": profile.get("target_keywords", [])}}
    with open(ROOT / "sources.yaml") as f:
        sources = yaml.safe_load(f)["sources"]

    db = ":memory:" if args.dry_run else DB_PATH
    conn = connect(db)

    # dry-run gets a throwaway budget file (a temp dir, deleted with the OS
    # temp cleanup) so exploratory/test runs never consume from the real
    # monthly counter -- same spirit as db=":memory:" above.
    if args.dry_run:
        import tempfile
        budget_path = Path(tempfile.mkdtemp()) / "serpapi_budget.json"
    else:
        budget_path = BUDGET_PATH
    serp_budget = SerpBudget(path=budget_path)

    print(f"fetching {len(sources)} sources...", file=sys.stderr)
    jobs = fetch_all(sources, serp_budget=serp_budget)
    print(f"  {len(jobs)} postings", file=sys.stderr)
    print(f"  serpapi budget: {serp_budget.used()}/{serp_budget.monthly_cap} used this month",
          file=sys.stderr)

    survivors = run_gates(jobs, tracks, conn)
    print(f"  {len(survivors)} passed gates (remote/EU/SMB/new)", file=sys.stderr)

    tracked = crm_tracked_keys()
    if tracked:
        before = len(survivors)
        survivors = drop_crm_tracked(survivors, tracked)
        print(f"  {len(survivors)} after CRM dedup (dropped {before - len(survivors)})", file=sys.stderr)

    client = build_azure_client()
    scored = []
    failures = 0
    for j in survivors:
        try:
            hint = smb_hint(j, conn)
            track = getattr(j, "track", "ai")
            r = score_job(profile, j.title, f"{j.location} [{hint}]", j.description,
                          client=client, track=track)
            s = int(r.get("score", 0))
            mark_seen(conn, j, score=s)
            if s >= args.min_score:
                scored.append({"job": j, "score": s, "reason": r.get("reason", ""),
                               "keywords": r.get("key_keywords_matched", []),
                               "track": track})
        except Exception as e:
            # A bad/expired credential fails EVERY job identically. Abort loudly instead
            # of grinding through the whole list and writing an empty queue.
            if _is_auth_error(e):
                sys.exit(
                    f"FATAL: Azure auth rejected while scoring ({e}). The scan cannot score any "
                    f"job, so it is aborting WITHOUT writing a queue (an empty queue is "
                    f"indistinguishable from 'no good jobs today'). Check AZURE_OPENAI_API_KEY "
                    f"in {ROOT / '.env'} -- the key was rotated on 2026-07-09."
                )
            failures += 1
            print(f"  score failed {j.org}/{j.title}: {e}", file=sys.stderr)

    # An empty output is not evidence of an empty input. If we scored nothing AND
    # everything we tried threw, this is an outage, not a quiet day.
    if survivors and not scored and failures == len(survivors):
        sys.exit(
            f"FATAL: all {failures} job(s) failed to score. Refusing to write an empty queue "
            f"that would look like a normal no-results day. See the errors above."
        )
    if failures:
        print(f"  WARNING: {failures} job(s) failed to score (kept {len(scored)})", file=sys.stderr)

    scored.sort(key=lambda s: s["score"], reverse=True)
    scored = scored[: args.max_results]

    today = date.today()
    scans_dir = ROOT / "scans"
    scans_dir.mkdir(exist_ok=True)
    md = format_digest(scored, scan_date=today)
    (scans_dir / f"{today.isoformat()}.md").write_text(md)
    (scans_dir / f"{today.isoformat()}.json").write_text(json.dumps(
        [{"score": s["score"], "reason": s["reason"], "keywords": s["keywords"],
          "job": {"source": s["job"].source, "org": s["job"].org, "title": s["job"].title,
                  "location": s["job"].location, "url": s["job"].url, "raw_id": s["job"].raw_id}}
         for s in scored], indent=2))

    queue_path = write_jobs_queue(scored, today)
    print(f"scout queue: {queue_path}", file=sys.stderr)

    if _auto_apply_enabled(dry_run=args.dry_run):
        auto_apply_pending(today.isoformat())

    print(f"\n{len(scored)} candidates >= {args.min_score}", file=sys.stderr)
    if args.dry_run:
        print(md)
        return
    if should_send(scored):
        tg = format_digest_for_telegram(scored, today.isoformat())
        print("telegram:", "sent" if send_text(tg) else "skipped", file=sys.stderr)
    else:
        print("telegram: quiet (no new qualifying roles today)", file=sys.stderr)


if __name__ == "__main__":
    main()
