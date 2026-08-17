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
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from cv_tailor.budget import JSearchBudget, SerpBudget
from cv_tailor.profile import load_profile
from cv_tailor.tailor_llm import build_azure_client
from cv_tailor.job_sources import fetch_all
from cv_tailor.match import score_job
from cv_tailor.digest import format_digest
from cv_tailor.sheets import get_pipeline_worksheet
from cv_tailor.telegram import format_digest_for_telegram, send_text


LOG_WIDTH = 90  # per-sample character cap, so one long title can't own the log
# The SAME counter file scripts/scan.py uses. A second file would let the two
# scanners each spend the 90/mo this repo takes out of the 250/mo SerpAPI pool
# it shares with norina-jobs.
BUDGET_PATH = ROOT / "data" / "serpapi_budget.json"
# Likewise the SAME jsearch counter scripts/scan.py uses: 6 keys x 200 req/mo
# = 1200/mo total, so a second file would let this scanner spend the daily
# scan's allowance over again.
JSEARCH_BUDGET_PATH = ROOT / "data" / "jsearch_budget.json"


def _log_safe(text, width: int = LOG_WIDTH) -> str:
    """Collapse whitespace and truncate an untrusted string for the scan log.

    Company names, titles AND exception texts come from strangers (anyone can
    post a job, and score_job is fed the posting's own description, so an
    HTTP/JSON error can echo it back verbatim). Without this, a newline in any
    of them lets a posting write its own line into the log a human reads.
    Twin of scripts/scan.py's _log_safe -- deliberately duplicated rather than
    imported, because scripts/ is not a package and this script's only job is
    to be independently runnable."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:width]


def _normalize(s: str) -> str:
    """Lowercase, strip whitespace, and remove non-alphanumeric so titles like
    'Full Stack Engineer Agent Tools' and 'Full Stack Engineer: Agent Tools'
    dedupe to the same key."""
    return re.sub(r"[^a-z0-9]+", "", s.strip().lower())


def already_tracked_keys(worksheet) -> set:
    """Return set of (company-normalized, role-normalized) already in the Sheet."""
    rows = worksheet.get_all_values()
    keys = set()
    for i, row in enumerate(rows):
        if i == 0 or len(row) < 2:
            continue
        keys.add((_normalize(row[0]), _normalize(row[1])))
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
    # ONE shared budget instance threaded through every serpapi source, exactly
    # as scripts/scan.py does it: this script reads the same sources.yaml, so
    # without it every serpapi query there fires unbudgeted against the capped
    # key. Its launchd plist is `.disabled` today, but an accidental run must
    # not be the expensive path.
    serp_budget = SerpBudget(path=BUDGET_PATH)
    jsearch_budget = JSearchBudget(path=JSEARCH_BUDGET_PATH)
    print(f"fetching {len(sources)} sources...", file=sys.stderr)
    jobs = fetch_all(sources, serp_budget=serp_budget, jsearch_budget=jsearch_budget)
    print(f"  got {len(jobs)} total postings", file=sys.stderr)
    print(f"  serpapi budget: {serp_budget.used()}/{serp_budget.monthly_cap} used this month",
          file=sys.stderr)
    print(f"  jsearch budget: {jsearch_budget.used()}/{jsearch_budget.monthly_cap} used this month",
          file=sys.stderr)

    # 2. Dedupe against Sheet
    if not args.no_dedupe:
        print("deduping against CRM Sheet...", file=sys.stderr)
        try:
            ws = get_pipeline_worksheet()
            tracked = already_tracked_keys(ws)
            before = len(jobs)
            jobs = [j for j in jobs if (_normalize(j.org), _normalize(j.title)) not in tracked]
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
            print(f"  score failed for {_log_safe(f'{j.org} / {j.title}')}: "
                  f"{_log_safe(str(e), width=300)}", file=sys.stderr)

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

    # 6. Telegram delivery (no-op if env vars not set)
    tg_text = format_digest_for_telegram(scored, today.isoformat())
    if send_text(tg_text):
        print("telegram: sent", file=sys.stderr)
    else:
        print("telegram: skipped (not configured or send failed)", file=sys.stderr)


if __name__ == "__main__":
    main()
