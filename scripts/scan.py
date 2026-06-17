# scripts/scan.py
#!/usr/bin/env python3
"""Daily job-discovery scanner (v2 funnel).

Pipeline: fetch -> Gate 1 (rules) -> Gate 2 (SMB provenance) -> Gate 3 (dedup vs
SQLite + CRM) -> LLM score survivors -> write digest -> quiet Telegram (only when
new qualifying roles exist). Scoring/tailoring/CRM unchanged.

Usage: python scripts/scan.py [--min-score 7] [--max-results 10] [--dry-run]
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
from cv_tailor.telegram import format_digest_for_telegram, send_text
from cv_tailor.cache import connect, is_new, mark_seen
from cv_tailor.gates import passes_gate1
from cv_tailor.enrich import is_smb, smb_hint

DB_PATH = ROOT / "data" / "jobs.db"


def run_gates(jobs, keywords, conn):
    """Gate 1 (rules) -> Gate 2 (SMB) -> Gate 3 (dedup). Returns survivors."""
    survivors = []
    for j in jobs:
        if not passes_gate1(j, keywords):
            continue
        if not is_smb(j):
            continue
        if not is_new(conn, j):
            continue
        survivors.append(j)
    return survivors


def should_send(scored):
    return len(scored) > 0


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--min-score", type=int, default=7)
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--dry-run", action="store_true", help="No Telegram, throwaway DB.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = load_profile("profile.yaml")
    keywords = profile.get("target_keywords", [])
    with open(ROOT / "sources.yaml") as f:
        sources = yaml.safe_load(f)["sources"]

    db = ":memory:" if args.dry_run else DB_PATH
    conn = connect(db)

    print(f"fetching {len(sources)} sources...", file=sys.stderr)
    jobs = fetch_all(sources)
    print(f"  {len(jobs)} postings", file=sys.stderr)

    survivors = run_gates(jobs, keywords, conn)
    print(f"  {len(survivors)} passed gates (remote/EU/SMB/new)", file=sys.stderr)

    client = build_azure_client()
    scored = []
    for j in survivors:
        try:
            hint = smb_hint(j)
            r = score_job(profile, j.title, f"{j.location} [{hint}]", j.description, client=client)
            s = int(r.get("score", 0))
            mark_seen(conn, j, score=s)
            if s >= args.min_score:
                scored.append({"job": j, "score": s, "reason": r.get("reason", ""),
                               "keywords": r.get("key_keywords_matched", [])})
        except Exception as e:
            print(f"  score failed {j.org}/{j.title}: {e}", file=sys.stderr)

    scored.sort(key=lambda s: s["score"], reverse=True)
    scored = scored[: args.max_results]

    today = date.today()
    scans_dir = ROOT / "scans"
    scans_dir.mkdir(exist_ok=True)
    md = format_digest(scored, scan_date=today)
    (scans_dir / f"{today.isoformat()}.md").write_text(md)
    (scans_dir / f"{today.isoformat()}.json").write_text(json.dumps(
        [{"score": s["score"], "reason": s["reason"], "keywords": s["keywords"],
          "job": {"source": s["job"].source, "org": s["job"].org, "title": s["job"].title,
                  "location": s["job"].location, "url": s["job"].url, "raw_id": s["job"].raw_id}}
         for s in scored], indent=2))

    print(f"\n{len(scored)} candidates >= {args.min_score}", file=sys.stderr)
    if args.dry_run:
        print(md)
        return
    if should_send(scored):
        tg = format_digest_for_telegram(scored, today.isoformat())
        print("telegram:", "sent" if send_text(tg) else "skipped", file=sys.stderr)
    else:
        print("telegram: quiet (no new qualifying roles today)", file=sys.stderr)


if __name__ == "__main__":
    main()
