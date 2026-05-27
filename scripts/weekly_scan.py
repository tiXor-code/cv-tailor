#!/usr/bin/env python3
"""Weekly job-discovery scanner.

Usage:
  python scripts/weekly_scan.py [--min-score 6] [--max-results 10]

Reads:
  - profile.yaml (canonical candidate profile)
  - sources.yaml (list of job sources to scan)
  - jobs CRM Sheet via gspread (for dedupe against already-tracked Company+Role)

Writes:
  - scans/<YYYY-MM-DD>.md (the digest)
  - scans/<YYYY-MM-DD>.json (raw scored results)
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from cv_tailor.profile import load_profile
from cv_tailor.tailor_llm import build_azure_client
from cv_tailor.job_sources import fetch_all
from cv_tailor.match import score_job
from cv_tailor.digest import format_digest
from cv_tailor.sheets import get_pipeline_worksheet


def already_tracked_keys(worksheet) -> set:
    """Return set of (company.lower().strip(), role.lower().strip()) already in the Sheet."""
    rows = worksheet.get_all_values()
    keys = set()
    for i, row in enumerate(rows):
        if i == 0 or len(row) < 2:
            continue
        keys.add((row[0].strip().lower(), row[1].strip().lower()))
    return keys


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--min-score", type=int, default=6)
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--no-dedupe", action="store_true",
                   help="Skip dedupe against CRM Sheet (faster for testing).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    profile = load_profile("profile.yaml")
    with open(ROOT / "sources.yaml") as f:
        sources_cfg = yaml.safe_load(f)
    sources = sources_cfg["sources"]

    # 1. Fetch
    print(f"fetching {len(sources)} sources...", file=sys.stderr)
    jobs = fetch_all(sources)
    print(f"  got {len(jobs)} total postings", file=sys.stderr)

    # 2. Dedupe against Sheet
    if not args.no_dedupe:
        print("deduping against CRM Sheet...", file=sys.stderr)
        try:
            ws = get_pipeline_worksheet()
            tracked = already_tracked_keys(ws)
            before = len(jobs)
            jobs = [j for j in jobs if (j.org.strip().lower(), j.title.strip().lower()) not in tracked]
            print(f"  dropped {before - len(jobs)} already-tracked; {len(jobs)} remain", file=sys.stderr)
        except Exception as e:
            print(f"warning: dedupe failed ({e}); continuing with all jobs", file=sys.stderr)

    # 3. Score
    print(f"scoring {len(jobs)} jobs via Azure OpenAI...", file=sys.stderr)
    client = build_azure_client()
    scored: list[dict] = []
    for j in jobs:
        try:
            r = score_job(profile, j.title, j.location, j.description, client=client)
            scored.append({
                "job": j,
                "score": int(r.get("score", 0)),
                "reason": r.get("reason", ""),
                "keywords": r.get("key_keywords_matched", []),
            })
        except Exception as e:
            print(f"  score failed for {j.org}/{j.title}: {e}", file=sys.stderr)

    # 4. Filter and sort
    scored = [s for s in scored if s["score"] >= args.min_score]
    scored.sort(key=lambda s: s["score"], reverse=True)
    scored = scored[: args.max_results]

    # 5. Write digest
    scans_dir = ROOT / "scans"
    scans_dir.mkdir(exist_ok=True)
    today = date.today()
    md_path = scans_dir / f"{today.isoformat()}.md"
    json_path = scans_dir / f"{today.isoformat()}.json"

    md = format_digest(scored, scan_date=today)
    md_path.write_text(md)
    json_path.write_text(json.dumps(
        [{"score": s["score"], "reason": s["reason"], "keywords": s["keywords"],
          "job": {"source": s["job"].source, "org": s["job"].org, "title": s["job"].title,
                  "location": s["job"].location, "url": s["job"].url, "raw_id": s["job"].raw_id}}
         for s in scored], indent=2))

    print(f"\ndigest written: {md_path}", file=sys.stderr)
    print(f"json written:   {json_path}", file=sys.stderr)
    print(f"\n{len(scored)} candidates passed threshold (score >= {args.min_score})", file=sys.stderr)


if __name__ == "__main__":
    main()
