"""Aggregator-to-ATS resolver: turn a job-board LISTING page URL (remoteOK,
WeWorkRemotely, ...) into the company's real ATS posting URL, so the portal
adapters can auto-apply.

Why: scan-time apply_target is just JobPosting.url (scout_queue.py:66), which
for aggregator-sourced jobs is the aggregator page -- no adapter claims those
hosts, so every such job dies needs_human("no-adapter") before a browser even
launches. Cohere (2026-07-10) arrived as a remoteOK link while Cohere actually
hires on Ashby, the one confirmed full-auto adapter.

Two strategies, in order:
  1. PAGE SCRAPE -- fetch the aggregator page and look for an outbound link
     whose host an adapter claims AND whose netloc+path contains the company
     slug (never link to a DIFFERENT company's ATS).
  2. BOARD PROBE -- try the company's slug on the Ashby/Greenhouse/Lever public
     board APIs (the scan's own fetchers, already tested) and match the job
     title. Ambiguity refuses to resolve: applying to the WRONG job is worse
     than staying needs_human.

Security rule (fix 1d9c700): every URL extracted from an untrusted page goes
through safe_hostname before any allowlist decision -- never bare urlparse.
"""
from __future__ import annotations

import re
import sys
import urllib.request
from urllib.parse import urlsplit

from cv_tailor.job_sources import (
    _normalize_org,
    fetch_ashby_org,
    fetch_greenhouse_org,
    fetch_lever_org,
)
from cv_tailor.urlsafe import host_matches, safe_hostname

_UA = "cv-tailor/0.3 (ats resolve; contact@teodorlutoiu.com)"
_TIMEOUT = 15
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)

# Fallback when the portal registry is unavailable (keeps unit tests free of
# playwright). The registry, when importable, is the source of truth.
_FALLBACK_HOSTS = ("jobs.ashbyhq.com", "boards.greenhouse.io",
                   "job-boards.greenhouse.io", "jobs.lever.co", "jobs.micro1.ai")


def adapter_hosts() -> tuple[str, ...]:
    try:
        from cv_tailor.portal.base import _REGISTRY  # lazy: imports playwright
        hosts: list[str] = []
        for adapter in _REGISTRY:
            hosts.extend(adapter.hosts)
        if hosts:
            return tuple(hosts)
    except Exception:  # noqa: BLE001 -- registry unavailable in light contexts
        pass
    return _FALLBACK_HOSTS


def _norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _title_match(wanted: str, candidate: str) -> bool:
    """Exact normalized equality, or containment when the shorter side is still
    specific enough (aggregators decorate titles with region/seniority tags)."""
    w, c = _norm_text(wanted), _norm_text(candidate)
    if not w or not c:
        return False
    if w == c:
        return True
    short = w if len(w) <= len(c) else c
    return len(short) >= 10 and (w in c or c in w)


def _adapter_claimed(url: str) -> bool:
    host = safe_hostname(url)
    return bool(host) and any(host_matches(host, a) for a in adapter_hosts())


def _fetch_page(url: str) -> str:
    """Degrade to empty on ANY failure -- a dead aggregator page must leave the
    job exactly as it was, never crash an apply run."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"[ats-resolve] page fetch failed: {type(exc).__name__}", file=sys.stderr)
        return ""


def resolve_from_page(html: str, company: str) -> str | None:
    """First outbound link on the page that an adapter claims and that belongs
    to THIS company (org slug must appear in the normalized netloc+path)."""
    org = _normalize_org(company)
    if not org:
        return None
    for href in _HREF_RE.findall(html or ""):
        href = href.strip()
        if href.startswith("//"):
            href = "https:" + href
        if not href.lower().startswith("http"):
            continue
        if not _adapter_claimed(href):
            continue
        parts = urlsplit(href)
        if org not in _norm_text(parts.netloc + parts.path):
            continue
        return href.split("#", 1)[0]
    return None


def _slug_candidates(company: str) -> list[str]:
    """Board-slug guesses derived ONLY from the company name."""
    out: list[str] = []
    org = _normalize_org(company)                      # "cohere", "kaipartners"
    hyphen = re.sub(r"[^a-z0-9]+", "-", (company or "").lower()).strip("-")
    for cand in (org, hyphen):                          # "kai-partners"
        if cand and cand not in out:
            out.append(cand)
    return out


# The whole pipeline exists to land an EU-remote role (Gate 1 enforces EU
# eligibility at scan time), so when a company posts the same role for several
# regions, the European posting is the only correct auto-apply target.
_REGION_PREFERENCE = ("europe", "emea", "unitedkingdom")


def _pick_match(matches: list, title: str) -> object | None:
    """Disambiguate multiple title matches. Order: unique exact title; unique
    region-preferred posting (title+location); else refuse -- applying to the
    WRONG posting is worse than staying needs_human."""
    if len(matches) == 1:
        return matches[0]
    exact = [j for j in matches if _norm_text(j.title) == _norm_text(title)]
    if len(exact) == 1:
        return exact[0]
    preferred = [
        j for j in matches
        if any(term in _norm_text(f"{j.title} {getattr(j, 'location', '')}")
               for term in _REGION_PREFERENCE)
    ]
    if len(preferred) == 1:
        return preferred[0]
    return None


def resolve_from_boards(company: str, title: str) -> str | None:
    for slug in _slug_candidates(company):
        for fetch in (fetch_ashby_org, fetch_greenhouse_org, fetch_lever_org):
            try:
                jobs = fetch(slug, company)
            except Exception:  # noqa: BLE001 -- unknown slug/host errors are normal
                jobs = []
            matches = [j for j in jobs if _title_match(title, j.title)]
            picked = _pick_match(matches, title) if matches else None
            if picked is not None and _adapter_claimed(picked.url):
                return picked.url
    return None


def resolve_ats_url(entry: dict) -> str | None:
    """The full resolution for one queue entry. None = leave the job unchanged."""
    company = (entry.get("company") or "").strip()
    title = (entry.get("title") or "").strip()
    if not company or not title:
        return None
    url = (entry.get("apply_target") or entry.get("url") or "").strip()
    if url:
        hit = resolve_from_page(_fetch_page(url), company)
        if hit:
            print(f"[ats-resolve] page link: {hit}", file=sys.stderr)
            return hit
    hit = resolve_from_boards(company, title)
    if hit:
        print(f"[ats-resolve] board probe: {hit}", file=sys.stderr)
    return hit
