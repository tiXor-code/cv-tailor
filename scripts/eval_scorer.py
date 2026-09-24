#!/usr/bin/env python3
"""Measure Scout's gate + scorer against postings Teodor rated himself.

The labeled set holds his yes/maybe/no marks and notes, so it lives OUTSIDE
this public repo (default ~/clawd/var/scout/calibration/labeled-*.json). This
calls the real scorer (Azure, a few cents), so it is a manual check to run
after any change to gates.py or match.SCORER_SYSTEM_PROMPT -- not a unit test.

"Surfaced" means what Scout would act on: passes Gate 1 and scores at least
AUTO_APPROVE_MIN. Agreement = his Yes surfaced, his No not surfaced; Maybe is
reported but not counted.

Usage:
  python scripts/eval_scorer.py [labeled.json ...]
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.answers import load_answers  # noqa: E402
from cv_tailor.autopilot import AUTO_APPROVE_MIN  # noqa: E402
from cv_tailor.gates import passes_gate1_tracks  # noqa: E402
from cv_tailor.job_sources import JobPosting  # noqa: E402
from cv_tailor.match import rank_ai_native, score_job  # noqa: E402
from cv_tailor.profile import load_profile  # noqa: E402
from cv_tailor.tailor_llm import build_azure_client  # noqa: E402

DEFAULT_GLOB = os.path.expanduser("~/clawd/var/scout/calibration/labeled-*.json")


def _score(profile, row, client, floor, tries=3):
    for attempt in range(tries):
        try:
            return score_job(profile, row["title"], row["location"], row["description"],
                             client=client, min_monthly_eur=floor)
        except Exception as exc:  # noqa: BLE001 -- retry transient API errors, then report
            if attempt == tries - 1:
                return {"score": None, "reason": f"error: {type(exc).__name__}"}
            time.sleep(2 * (attempt + 1))


def main(argv=None) -> int:
    paths = (argv if argv is not None else sys.argv[1:]) or sorted(glob.glob(DEFAULT_GLOB))
    rows = [r for p in paths for r in json.load(open(p))]
    if not rows:
        print("no labeled postings found", file=sys.stderr)
        return 2
    profile = load_profile(ROOT / "profile.yaml", strict=True)
    tracks = profile["tracks"]
    client = build_azure_client()
    floor = (load_answers() or {}).get("salary_fulltime_gross_eur_month")
    agree = total = 0
    misses = []
    for row in rows:
        job = JobPosting(source=row.get("source", ""), org=row["company"], title=row["title"],
                         location=row["location"], url="", description=row["description"], raw_id=row["id"])
        if passes_gate1_tracks(job, tracks) is None:
            score, reason = 0, "dropped at gate 1"
        else:
            r = _score(profile, row, client, int(floor) if floor else None)
            score, reason = r.get("score"), r.get("reason", "")
            if isinstance(score, int):
                score = rank_ai_native(score, row["description"])
        surfaced = score is not None and score >= AUTO_APPROVE_MIN
        label = row.get("label")
        if label in ("yes", "no"):
            total += 1
            ok = surfaced == (label == "yes")
            agree += ok
            if not ok:
                misses.append((label, score, row["company"], row["title"], reason, row.get("note", "")))
        print(f"{label or '-':5} score={score!s:4} {'SURFACED' if surfaced else '        '} "
              f"{row['company']} / {row['title']}")
    print(f"\nagreement on yes/no: {agree}/{total}")
    for label, score, company, title, reason, note in misses:
        print(f"  MISS {label} score={score} {company} / {title}\n     scorer: {reason}\n     his note: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
