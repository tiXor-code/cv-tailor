#!/usr/bin/env python3
"""Record Teodor's answer on a supervised LinkedIn handoff.

Spawned by mac-sidecar's POST /admin/scout/decide when he taps Applied or
Not applying on admin.teodorlutoiu.com/scout. Only a `handed_off` entry can be
ticked (a compare-and-set, so a double tap or a stale page changes nothing).

  applied       -> ledger row (channel linkedin-manual) so no source ever offers
                   that company|role again, CRM row marked Applied, status
                   applied_by_hand. The row does NOT count against Scout's own
                   APPLY_DAILY_CAP (cache.applications_sent_today).
  not_applying  -> status not_applying, no ledger row.

Exit codes: 0 done, 2 bad arguments, 3 not in handed_off (entry untouched),
4 unknown job.

Usage:
  python scripts/mark_handoff.py --scan-date D --job-id X --outcome applied|not_applying
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.cache import MANUAL_CHANNEL, application_exists, connect, record_application  # noqa: E402
from cv_tailor.scout_queue import StatusConflict, update_entry  # noqa: E402
from cv_tailor.sheets import crm_mark_applied  # noqa: E402

DEFAULT_DB_PATH = ROOT / "data" / "jobs.db"
_OUTCOMES = {"applied": "applied_by_hand", "not_applying": "not_applying"}


def _db_path() -> Path:
    env = os.environ.get("SCOUT_DB_PATH")
    return Path(env) if env else DEFAULT_DB_PATH


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-date", required=True)
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--outcome", required=True, choices=sorted(_OUTCOMES))
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2

    now = datetime.now(timezone.utc).isoformat()
    status = _OUTCOMES[args.outcome]

    def _mut(e: dict) -> None:
        e["status"] = status
        e["decided_at"] = now
        if status == "applied_by_hand":
            e["applied_at"] = now

    try:
        entry = update_entry(args.scan_date, args.job_id, _mut, expect_status="handed_off")
    except StatusConflict as exc:
        print(f"not a handed-off job: {exc}", file=sys.stderr)
        return 3
    except (KeyError, FileNotFoundError) as exc:
        print(f"unknown job: {exc}", file=sys.stderr)
        return 4

    if status == "applied_by_hand":
        company, role = entry.get("company", ""), entry.get("title", "")
        conn = connect(_db_path())
        # An existing row for this job_id means a retried tick: nothing to add.
        if not application_exists(conn, job_id=args.job_id, company=company, role=role):
            record_application(conn, job_id=args.job_id, company=company, role=role,
                               url=entry.get("url", ""), channel=MANUAL_CHANNEL)
        try:
            crm_mark_applied(company, role, entry.get("url", ""))
        except Exception as exc:  # noqa: BLE001 -- he applied; a Sheets hiccup must not undo it
            print(f"crm mark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"{args.job_id}: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
