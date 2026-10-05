#!/usr/bin/env python3
"""Record what happened after an application went out.

Only an entry that reached an employer (status sent or applied_by_hand) has an
outcome. The entry gains outcome / outcome_at / outcome_history (and
followed_up_at with --followed-up); the ledger row in data/jobs.db mirrors the
outcome when one exists. Moving to `interview` writes interview_prep.md into
the package dir (skip with --no-prep); a prep failure never undoes the outcome.

Exit codes: 0 done, 2 bad arguments, 3 not an applied job (entry untouched),
4 unknown job.

Usage:
  python scripts/mark_outcome.py DATE JOB_ID [OUTCOME] [--note TEXT] [--followed-up] [--no-prep]
  OUTCOME: no_reply | replied | interview | rejected | offer | withdrawn
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.cache import connect, set_application_outcome  # noqa: E402
from cv_tailor.tracker import OUTCOMES, NotApplied, set_outcome  # noqa: E402

DEFAULT_DB_PATH = ROOT / "data" / "jobs.db"


def _db_path() -> Path:
    env = os.environ.get("SCOUT_DB_PATH")
    return Path(env) if env else DEFAULT_DB_PATH


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass


def run_prep(scan_date: str, job_id: str) -> Path:
    """Seam for tests: the interview pack for this job (LLM call inside)."""
    from cv_tailor.interview_prep import prepare
    from cv_tailor.offer_eval import load_yaml
    from cv_tailor.profile import load_profile
    _load_dotenv()
    profile = load_profile(Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml")))
    answers = load_yaml(Path(os.environ.get("CV_TAILOR_ANSWERS", ROOT / "answers.yaml")))
    return prepare(scan_date, job_id, profile=profile, answers=answers)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record an application outcome")
    ap.add_argument("scan_date")
    ap.add_argument("job_id")
    ap.add_argument("outcome", nargs="?", choices=OUTCOMES)
    ap.add_argument("--note", default=None)
    ap.add_argument("--followed-up", action="store_true",
                    help="he chased the employer; drops it off the follow-up list")
    ap.add_argument("--no-prep", action="store_true",
                    help="do not write interview_prep.md on a move to interview")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    if args.outcome is None and not args.followed_up and args.note is None:
        print("nothing to record: give an outcome, --followed-up or --note", file=sys.stderr)
        return 2

    try:
        entry, previous = set_outcome(args.scan_date, args.job_id, args.outcome,
                                      note=args.note, followed_up=args.followed_up)
    except NotApplied as exc:
        print(f"not an applied job: {exc}", file=sys.stderr)
        return 3
    except (KeyError, FileNotFoundError) as exc:
        print(f"unknown job: {exc}", file=sys.stderr)
        return 4
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.outcome and args.outcome != previous:
        try:
            db = _db_path()
            if db.exists():
                conn = connect(db)
                try:
                    set_application_outcome(conn, job_id=args.job_id, outcome=args.outcome,
                                            at=entry.get("outcome_at") or "")
                finally:
                    conn.close()
        except Exception as exc:  # noqa: BLE001 -- the queue entry is the record of truth
            print(f"ledger mirror failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"{args.job_id}: {entry.get('outcome')}"
          + (" (followed up)" if args.followed_up else ""))

    if args.outcome == "interview" and previous != "interview" and not args.no_prep:
        try:
            path = run_prep(args.scan_date, args.job_id)
            print(f"interview prep: {path}")
        except Exception as exc:  # noqa: BLE001 -- prep is a bonus; the outcome stands
            print(f"interview prep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
