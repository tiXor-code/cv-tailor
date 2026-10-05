#!/usr/bin/env python3
"""Value an offer against the floors in answers.yaml and draft a counter.

stdin: the offer terms, as JSON (keys: base_monthly | base_annual,
months_per_year, bonus_guaranteed_annual | bonus_guaranteed_pct,
bonus_target_annual | bonus_target_pct, benefits_monthly, equipment_one_off,
remote_pct, pto_days, notice_days, employment employee|contractor, currency,
role, company) or loose text lines ("Base: 4,500 EUR/month", "Remote: 80%").

Prints markdown (default) or JSON (--json); --out-md / --out-json also write
files. Nothing is sent anywhere: the counter is a draft for him.

Usage:
  python scripts/offer_eval.py [--json] [--out-md PATH] [--out-json PATH] < offer.txt
Exit codes: 0 done, 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.offer_eval import evaluate, load_yaml, to_markdown  # noqa: E402
from cv_tailor.profile import load_profile  # noqa: E402


def main(argv=None, stdin=None) -> int:
    ap = argparse.ArgumentParser(description="Offer evaluation")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--out-json", default=None)
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    raw = (stdin or sys.stdin).read()
    if not raw.strip():
        print("error: no offer on stdin", file=sys.stderr)
        return 2
    profile = load_profile(Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml")))
    answers = load_yaml(Path(os.environ.get("CV_TAILOR_ANSWERS", ROOT / "answers.yaml")))
    try:
        result = evaluate(raw, answers, profile)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    md = to_markdown(result)
    if args.out_md:
        Path(args.out_md).write_text(md)
    if args.out_json:
        Path(args.out_json).write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
