"""Ashby board harvester.

Turns companies the scan has ALREADY seen into standing, keyless sources on
the one ATS whose portal adapter completes end-to-end.

Why Ashby first: it is the only confirmed full-auto adapter (greenhouse and
lever wall at a captcha), so supply landed there is the supply that can
actually become an autonomous submission.

Measured 2026-09-16 against the live board API: of the 98 companies that had
ever scored >= 6, twenty-three have a live Ashby board, carrying 1,238 open
roles between them -- against the five ashby slugs sources.yaml configures.
"""
from __future__ import annotations

import re

from cv_tailor.job_sources import fetch_ashby_org

# Aggregators wearing an employer's clothes. An aggregator board is not an
# employer board: enrolling one pours thousands of aggregator-quality
# postings into a scan that sees ~11 new postings a day, burning scoring
# spend on jobs that never clear the floor -- the same failure mode as the
# arbeitnow.com/JOB_BOARD_DOMAINS bug. The board API cannot tell us which is
# which, so this list is the only guard, and it is matched on the DERIVED
# SLUG so a company's display spelling cannot slip past it.
DENIED_SLUGS = frozenset({
    "jobgether", "jobright", "remoterocketship", "wellfound", "otta",
    "welcometothejungle", "hired", "ziprecruiter", "indeed", "linkedin",
})


def validate_ashby_slug(slug: str) -> bool:
    """True iff `slug` names a board that currently has open roles.

    Validity is len(jobs) > 0, NEVER the HTTP status: a slug with no board at
    all answers HTTP 200 with jobs: [] (verified live on 'vercel'), so reading
    the status would enrol every typo as a source.

    Fails closed on any probe failure -- a timeout says nothing about whether
    the board exists, and an unreachable probe must never enrol a source.
    """
    if not slug:
        return False
    try:
        return len(fetch_ashby_org(slug)) > 0
    except Exception:       # noqa: BLE001 -- unknown slugs 404, networks flake
        return False


def _slug_candidates(company: str) -> list[str]:
    """Board-slug guesses derived ONLY from the company name, in the two
    shapes Ashby orgs actually use: squashed ("roompricegenie") and
    hyphenated ("lemon-io"). Both were observed live on 2026-09-16."""
    low = (company or "").lower()
    squashed = re.sub(r"[^a-z0-9]+", "", low)
    hyphenated = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
    return [s for s in dict.fromkeys([squashed, hyphenated]) if s]


def harvest_ashby_slugs(companies, known_slugs=()) -> list[str]:
    """Validated, newly-found Ashby slugs for `companies`, in first-seen order.

    Skips, WITHOUT probing: slugs already configured (enrolment must be
    idempotent -- a slug enrolling twice, or silently replacing an existing
    entry, has to be impossible), denied aggregator slugs, and any slug
    already probed this pass. The pool is every company the scan has ever
    seen, so a duplicate probe is a duplicate request against someone else's
    API for an answer we already have.

    Stops at the first candidate shape that validates: one board per company.
    """
    known = {str(s).strip().lower() for s in (known_slugs or ())}
    found: list[str] = []
    probed: set[str] = set()
    for name in companies or ():
        for slug in _slug_candidates(name):
            if slug in probed or slug in known or slug in DENIED_SLUGS:
                continue
            probed.add(slug)
            if validate_ashby_slug(slug):
                found.append(slug)
                break
    return found
