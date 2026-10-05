"""Attach the vetting results to Scout queue entries.

Three fields, written onto jobs.json entries (the admin /scout UI renders
whatever is there):

  listing_fitness  {score, band, flags}            vetting.vet_listing, every queued job
  pay              {stated, estimate, basis, verdict, compared_to, contract}
                                                   pay_check; the LLM estimate only for
                                                   jobs approved or on his list
  company_check    {verdict, summary, evidence, checked_at, own_site_lists_role}
                                                   company_check; only for jobs on his list
                                                   scoring >= COMPANY_CHECK_MIN_SCORE
  listing_warning  str, only on a high-risk job that is on his list anyway

Every enrich_* call is best-effort: it never raises and never changes an
entry's status, so it can sit inside the apply orchestrator without being
able to break an application. SCOUT_ENRICH_DISABLED=1 turns the network-using
calls off (the test suite sets it; a subprocess inherits it).
"""
from __future__ import annotations

import json
import os
import sys

from cv_tailor import company_check as cc
from cv_tailor import pay_check
from cv_tailor.scout_queue import queue_root, read_description, update_entry
from cv_tailor.vetting import band_for, red_flag_summary, vet_listing

COMPANY_CHECK_MIN_SCORE = 8
DISABLED_ENV = "SCOUT_ENRICH_DISABLED"


def enabled() -> bool:
    return os.environ.get(DISABLED_ENV, "") != "1"


def scan_fields(description: str, floors: dict) -> dict:
    """The no-network fields, computed for every job the scan queues."""
    return {"listing_fitness": vet_listing(description),
            "pay": pay_check.price_stated(description, floors)}


def high_risk_warning(fitness: dict) -> str:
    return (f"High-risk listing (fitness {fitness.get('score')}/100: {red_flag_summary(fitness)}). "
            f"Check the company before applying.")


def is_high_risk(fitness: dict | None) -> bool:
    return bool(fitness) and (fitness.get("band") == "high_risk"
                              or band_for(int(fitness.get("score", 100))) == "high_risk")


def _load_entry(scan_date: str, job_id: str, queue_dir=None) -> dict | None:
    try:
        entries = json.loads((queue_root(queue_dir) / scan_date / "jobs.json").read_text())
    except (OSError, ValueError):
        return None
    return next((e for e in entries if e.get("id") == job_id), None)


def _has_priced(pay: dict | None) -> bool:
    return bool(pay) and bool(pay.get("stated") or pay.get("estimate"))


def _log(msg: str) -> None:
    print(f"enrich: {msg}", file=sys.stderr)


def enrich_pay(scan_date: str, job_id: str, *, queue_dir=None, client=None,
               floors: dict | None = None, allow_llm: bool = True) -> dict | None:
    """Fill `pay` (and `listing_fitness` if missing) for one entry. The LLM
    estimate runs only when no pay is stated and none was estimated before."""
    try:
        entry = _load_entry(scan_date, job_id, queue_dir)
        if entry is None:
            return None
        desc = read_description(scan_date, job_id, queue_dir=queue_dir)
        floors = floors if floors is not None else pay_check.load_floors()
        updates: dict = {}
        if not entry.get("listing_fitness") and desc:
            updates["listing_fitness"] = vet_listing(desc)
        pay = entry.get("pay")
        if not _has_priced(pay):
            pay = pay_check.price_role(
                title=entry.get("title", ""), location=entry.get("location", ""),
                description=desc or f"{entry.get('title', '')} at {entry.get('company', '')}",
                company=entry.get("company", ""), floors=floors,
                allow_llm=allow_llm and enabled(), client=client)
            updates["pay"] = pay
        if updates:
            update_entry(scan_date, job_id, lambda e: e.update(updates), queue_dir=queue_dir)
        return pay
    except Exception as exc:  # noqa: BLE001 -- enrichment must never break the caller
        _log(f"pay {job_id}: {type(exc).__name__}")
        return None


def enrich_company(scan_date: str, job_id: str, *, queue_dir=None, client=None, search=None,
                   budget=None, cache=None, min_score: int = COMPANY_CHECK_MIN_SCORE,
                   now=None) -> tuple[dict | None, str]:
    """Run the company background check for one entry on his list with a
    score >= min_score. Returns (record, note)."""
    try:
        entry = _load_entry(scan_date, job_id, queue_dir)
        if entry is None:
            return None, "no-entry"
        if entry.get("company_check"):
            return entry["company_check"], "already-checked"
        try:
            score = int(entry.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        if score < min_score:
            return None, "below-score"
        if search is None and not enabled():
            return None, "disabled"
        record, note = cc.check_company(
            entry.get("company", ""), entry.get("title", ""), client=client, search=search,
            budget=budget, cache=cache, queue_dir=queue_dir, now=now)
        if record is not None:
            update_entry(scan_date, job_id, lambda e: e.update(company_check=record),
                         queue_dir=queue_dir)
        return record, note
    except Exception as exc:  # noqa: BLE001
        _log(f"company {job_id}: {type(exc).__name__}")
        return None, "error"


def mark_listing_warning(scan_date: str, job_id: str, *, queue_dir=None) -> str | None:
    """Put `listing_warning` on an entry whose listing is high risk."""
    try:
        entry = _load_entry(scan_date, job_id, queue_dir)
        fitness = (entry or {}).get("listing_fitness")
        if not is_high_risk(fitness):
            return None
        warning = high_risk_warning(fitness)
        if entry.get("listing_warning") != warning:
            update_entry(scan_date, job_id, lambda e: e.update(listing_warning=warning),
                         queue_dir=queue_dir)
        return warning
    except Exception as exc:  # noqa: BLE001
        _log(f"warning {job_id}: {type(exc).__name__}")
        return None


def enrich_approved(scan_date: str, job_id: str, **kw) -> None:
    """Hook for an approved job (autopilot or his tap): price it."""
    if not enabled():
        return
    enrich_pay(scan_date, job_id, **kw)


def enrich_listed(scan_date: str, job_id: str, *, queue_dir=None, client=None, **company_kw) -> None:
    """Hook for a job that just landed on his apply-yourself list: pay (if
    not priced yet), the high-risk warning, and the company check."""
    if not enabled():
        return
    enrich_pay(scan_date, job_id, queue_dir=queue_dir, client=client)
    mark_listing_warning(scan_date, job_id, queue_dir=queue_dir)
    enrich_company(scan_date, job_id, queue_dir=queue_dir, client=client, **company_kw)
