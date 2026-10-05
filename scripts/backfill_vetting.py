#!/usr/bin/env python3
"""Backfill listing_fitness, pay and company_check onto his apply-yourself list.

Walks the last --days day directories of the Scout queue and, for every entry
with status `handed_off`:
  - listing_fitness  from descriptions.json (free), plus listing_warning when
                     the listing reads high risk. The status is NOT changed:
                     he may already be mid-application.
  - pay              stated pay (free); the LLM estimate when none is stated
  - company_check    for score >= 8, within the company-check SerpAPI budget
                     (10/day, 90/month by default) and the 30-day company cache

--dry-run reads only: it prints what each entry would get and what the
company check would cost, and makes no LLM call, no search and no write.

Usage:
  python scripts/backfill_vetting.py [--dry-run] [--days 14] [--no-company] [--no-llm]
Env: SCOUT_QUEUE_DIR overrides the queue root.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor import company_check as cc  # noqa: E402
from cv_tailor import listing_enrich, pay_check  # noqa: E402
from cv_tailor.scout_queue import queue_root, read_description  # noqa: E402
from cv_tailor.vetting import vet_listing  # noqa: E402


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


def listed_entries(days: int, *, queue_dir=None, today: date | None = None) -> list[tuple[str, dict]]:
    today = today or date.today()
    root = queue_root(queue_dir)
    out = []
    for back in range(days):
        day = (today - timedelta(days=back)).isoformat()
        try:
            entries = json.loads((root / day / "jobs.json").read_text())
        except (OSError, ValueError):
            continue
        out.extend((day, e) for e in entries if e.get("status") == "handed_off")
    return out


def _plan(day: str, e: dict, *, queue_dir=None, floors: dict) -> dict:
    """What the backfill would do for one entry, computed without network."""
    desc = read_description(day, e["id"], queue_dir=queue_dir)
    fitness = e.get("listing_fitness") or (vet_listing(desc) if desc else None)
    pay = e.get("pay") or pay_check.price_stated(desc, floors)
    try:
        score = int(e.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    company = None
    if not e.get("company_check") and score >= listing_enrich.COMPANY_CHECK_MIN_SCORE:
        cached = cc.CompanyCache(queue_dir=queue_dir).get(e.get("company", ""))
        company = "cached" if cached else f"{cc.SEARCHES_PER_CHECK} searches"
    return {
        "fitness": fitness,
        "needs_llm_pay": not (pay.get("stated") or pay.get("estimate")),
        "company": company,
    }


def main(argv=None, *, client=None, search=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--no-company", action="store_true", help="skip the company check")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM pay estimate")
    args = ap.parse_args(argv)
    if not args.dry_run:
        _load_dotenv()

    floors = pay_check.load_floors()
    rows = listed_entries(args.days)
    print(f"{len(rows)} job(s) on his list in the last {args.days} day(s)")
    budget = cc.CompanyCheckBudget()
    if not args.dry_run and not listing_enrich.enabled():
        print(f"{listing_enrich.DISABLED_ENV}=1: network calls are off", file=sys.stderr)

    searches_planned = 0
    for day, e in rows:
        # Company/title come from strangers: collapse whitespace so a posting
        # cannot forge its own lines in this output.
        label = " ".join(f"{day} {e.get('company')} / {e.get('title')}".split())[:100]
        plan = _plan(day, e, floors=floors)
        fit = plan["fitness"]
        fit_s = f"{fit['score']} {fit['band']}" if fit else "no description"
        if args.dry_run:
            if plan["company"] and plan["company"] != "cached":
                searches_planned += cc.SEARCHES_PER_CHECK
            print(f"- {label}: fitness {fit_s}; pay estimate "
                  f"{'LLM' if plan['needs_llm_pay'] and not args.no_llm else 'not needed'}; "
                  f"company check {plan['company'] or 'not needed'}")
            continue

        listing_enrich.enrich_pay(day, e["id"], client=client, floors=floors,
                                  allow_llm=not args.no_llm)
        warning = listing_enrich.mark_listing_warning(day, e["id"])
        note = "skipped"
        if not args.no_company:
            _, note = listing_enrich.enrich_company(day, e["id"], client=client, search=search,
                                                   budget=budget)
        print(f"- {label}: fitness {fit_s}{' WARNING' if warning else ''}; company {note}")

    if args.dry_run:
        print(f"dry run: would use up to {searches_planned} company-check search(es); budget "
              f"{budget.used()}/{budget.monthly_cap} this month, {budget.day_used()}/{budget.daily_cap} today")
    else:
        print(f"company-check budget: {budget.used()}/{budget.monthly_cap} this month, "
              f"{budget.day_used()}/{budget.daily_cap} today")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
