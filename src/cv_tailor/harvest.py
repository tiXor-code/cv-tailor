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

from cv_tailor.job_sources import fetch_ashby_org


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
