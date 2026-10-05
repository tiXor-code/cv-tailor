#!/usr/bin/env python3
"""Scout tracker: statuses, outcomes, response rates, follow-ups, stale waits.

Reads every day under the queue root (SCOUT_QUEUE_DIR or ~/clawd/var/scout),
never writes the queue. Writes the report as JSON (default
<queue root>/tracker.json) and prints a readable summary.

Usage:
  python scripts/tracker_report.py [--out PATH] [--no-write] [--json]
                                   [--follow-up-days N] [--stale-days N]
Exit codes: 0 done, 2 bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.scout_queue import queue_root  # noqa: E402
from cv_tailor.tracker import (  # noqa: E402
    FOLLOW_UP_AFTER_DAYS, STALE_AFTER_DAYS, build_report, format_report,
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scout tracker report")
    ap.add_argument("--out", default=None, help="JSON path (default <queue root>/tracker.json)")
    ap.add_argument("--no-write", action="store_true", help="print only")
    ap.add_argument("--json", action="store_true", help="print the JSON instead of the summary")
    ap.add_argument("--follow-up-days", type=int, default=FOLLOW_UP_AFTER_DAYS)
    ap.add_argument("--stale-days", type=int, default=STALE_AFTER_DAYS)
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    report = build_report(follow_up_days=args.follow_up_days, stale_days=args.stale_days)
    if not args.no_write:
        out = Path(args.out) if args.out else queue_root() / "tracker.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        os.replace(tmp, out)
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
