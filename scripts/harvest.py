#!/usr/bin/env python3
"""Ashby board harvester CLI -- gated wrapper around cv_tailor.harvest.

Invoked unconditionally by scripts/run_scan.sh BEFORE the daily scan, so any
board enrolled this morning is already a source for this morning's run. The
SCOUT_HARVEST env flag (not the caller) decides whether anything happens, so
disabling the harvester is one .env edit and never a launchd change -- the
same arrangement as scripts/autopilot.py.

Exit code is ALWAYS 0: this runs ahead of the scan, and a dead board API or a
locked database must never stop the day's scan and autopilot from happening.

Usage: python scripts/harvest.py [--limit N] [--disable SLUG]
Env: SCOUT_HARVEST=1 enables. SCOUT_DB_PATH overrides the seen-jobs database.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from cv_tailor.harvest import (  # noqa: E402
    DEFAULT_PROBE_LIMIT,
    disable_ashby_slug,
    harvest_and_enrol,
)

REGISTRY = ROOT / "data" / "sources_harvested.yaml"


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """Best-effort bootstrap; explicit environment always wins (setdefault)."""
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
    return Path(env) if env else ROOT / "data" / "jobs.db"


def seen_companies() -> list[str]:
    """Every company the scan has ever seen, oldest first so the backlog
    drains in a stable order across capped runs. A missing or unreadable
    database is not an error -- it just means nothing to harvest yet."""
    path = _db_path()
    if not path.exists():
        return []
    try:
        with sqlite3.connect(path) as conn:
            return [r[0] for r in conn.execute(
                "select company from seen_jobs where company != '' "
                "group by company order by min(first_seen)")]
    except sqlite3.Error:
        return []


def configured_ashby_slugs() -> list[str]:
    """Slugs already in the hand-curated sources.yaml, so the harvester never
    re-probes or duplicates one of them."""
    try:
        doc = yaml.safe_load((ROOT / "sources.yaml").read_text()) or {}
    except OSError:
        return []
    return [str(s.get("slug", "")) for s in (doc.get("sources") or [])
            if isinstance(s, dict) and s.get("kind") == "ashby"]


def main(argv=None) -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Ashby board harvest pass")
    ap.add_argument("--limit", type=int, default=DEFAULT_PROBE_LIMIT,
                    help=f"max board probes this run (default {DEFAULT_PROBE_LIMIT})")
    ap.add_argument("--disable", metavar="SLUG", default=None,
                    help="de-enrol a board permanently; it can never be re-enrolled")
    args = ap.parse_args(argv)

    if os.environ.get("SCOUT_HARVEST", "0") != "1":
        print("SCOUT_HARVEST != 1: harvester disabled, nothing probed.")
        return 0

    if args.disable:
        try:
            disable_ashby_slug(REGISTRY, args.disable)
            print(f"harvest: disabled {args.disable}")
        except Exception as e:      # noqa: BLE001 -- never kill the daily run
            print(f"harvest failed while disabling {args.disable}: {e}")
        return 0

    try:
        companies = seen_companies()
        added = harvest_and_enrol(REGISTRY, companies,
                                  known_slugs=configured_ashby_slugs(),
                                  limit=args.limit)
    except Exception as e:          # noqa: BLE001 -- never kill the daily run
        print(f"harvest failed: {e}")
        return 0

    print(f"harvest: companies={len(companies)} enrolled={len(added)}"
          + (f" [{', '.join(added)}]" if added else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
