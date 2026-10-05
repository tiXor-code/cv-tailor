#!/usr/bin/env python3
"""Write interview_prep.md (+ interview_prep.json) for one queued job.

Built from profile.yaml, the archived JD (descriptions.json) and the cover
letter that went out; every STAR story and probe defence is checked against
the profile item it cites, and anything ungrounded is moved to the gaps list.
Salary rehearsal comes from answers.yaml floors (unknown when a floor is not
set). Runs automatically when scripts/mark_outcome.py records `interview`.

Usage:
  python scripts/interview_prep.py DATE JOB_ID [--force]
Exit codes: 0 written (path printed), 2 bad arguments, 4 unknown job, 1 failed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.interview_prep import prepare  # noqa: E402
from cv_tailor.offer_eval import load_yaml  # noqa: E402
from cv_tailor.profile import load_profile  # noqa: E402


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


def main(argv=None, *, client=None) -> int:
    ap = argparse.ArgumentParser(description="Interview prep pack for one job")
    ap.add_argument("scan_date")
    ap.add_argument("job_id")
    ap.add_argument("--force", action="store_true", help="rewrite an existing pack")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    _load_dotenv()
    profile = load_profile(Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml")))
    answers = load_yaml(Path(os.environ.get("CV_TAILOR_ANSWERS", ROOT / "answers.yaml")))
    try:
        path = prepare(args.scan_date, args.job_id, profile=profile, answers=answers,
                       client=client, force=args.force)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (KeyError, FileNotFoundError) as exc:
        print(f"unknown job: {exc}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"interview prep failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
