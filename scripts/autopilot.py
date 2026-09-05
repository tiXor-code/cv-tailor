#!/usr/bin/env python3
"""Scout autopilot CLI -- gated wrapper around cv_tailor.autopilot.

Invoked unconditionally by scripts/run_scan.sh right after the daily scan;
the SCOUT_AUTOPILOT env flag (not the caller) decides whether anything
happens, so disabling autopilot is one .env edit and never a launchd change.

Exit codes: 0 = pass completed (individual job failures are queue statuses +
digest lines, not process failures) or gate off; 2 = bad arguments.

Usage: python scripts/autopilot.py [--date YYYY-MM-DD] [--no-telegram]
Env: SCOUT_AUTOPILOT=1 enables. SCOUT_QUEUE_DIR overrides the queue root
(tests). APPLY_ARMED / APPLY_DAILY_CAP are read downstream in
scripts/apply_approved.py, never here.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.autopilot import run_autopilot  # noqa: E402
from cv_tailor.scout_queue import _validated_day  # noqa: E402
from cv_tailor.telegram import send_text  # noqa: E402


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Same best-effort bootstrap as scripts/apply_approved.py: explicit
    environment always wins (setdefault)."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def main(argv=None) -> int:
    _load_dotenv()
    if os.environ.get("SCOUT_AUTOPILOT", "0") != "1":
        print("SCOUT_AUTOPILOT != 1: autopilot disabled, nothing done.")
        return 0

    ap = argparse.ArgumentParser(description="Scout autopilot pass")
    ap.add_argument("--date", default=None,
                    help="treat this YYYY-MM-DD as 'today' (reruns/tests)")
    ap.add_argument("--no-telegram", action="store_true",
                    help="skip the digest message")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    if args.date:
        try:
            day = _validated_day(args.date)  # raises ValueError on garbage
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
        now = datetime.fromisoformat(day).replace(
            hour=now.hour, minute=now.minute, tzinfo=timezone.utc)

    report = run_autopilot(now, notify=None if args.no_telegram else send_text)
    print(
        f"autopilot: applied={len(report.applied)} parked={len(report.parked)} "
        f"failed={len(report.failed)} queued_new={len(report.queued_new)} "
        f"expired={len(report.expired)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
