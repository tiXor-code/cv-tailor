"""Aggregator-to-ATS resolver: turn a job-board LISTING page URL (remoteOK,
WeWorkRemotely, ...) into the company's real ATS posting URL, so the portal
adapters can auto-apply.

Why: scan-time apply_target is just JobPosting.url (scout_queue.py:66), which
for aggregator-sourced jobs is the aggregator page -- no adapter claims those
hosts, so every such job dies needs_human("no-adapter") before a browser even
launches. Cohere (2026-07-10) arrived as a remoteOK link while Cohere actually
hires on Ashby, the one confirmed full-auto adapter.

Three strategies, in order:
  0. APPLY OPTIONS -- the apply links the source already handed us (stored on
     the queue entry as apply_options). No network at all; same org-match rule
     as the page scrape, because those links are exactly the ones
     _best_company_url declined to trust blindly.
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
                   "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io",
                   "jobs.lever.co", "jobs.micro1.ai")


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


def resolve_from_apply_options(entry: dict) -> str | None:
    """Strategy 0: the apply links the source already handed us.

    scout_queue stores every apply link SerpAPI returned (entry["apply_options"]),
    including the ones _best_company_url rejected when it fell back to the Google
    Jobs share_link. An adapter-claimed link in that list is the cheapest possible
    resolution -- no network, no scrape of a bot-walled SERP.

    The org check is NOT optional. _best_company_url rejected these links for a
    reason: a SerpAPI card can carry a DIFFERENT company's ATS link (an
    EnthuZiastic posting whose only Workday link was Cisco's). So a link only
    resolves when an adapter claims its host AND the normalized org name appears
    in the host+path. Everything else stays None and reaches a human instead --
    /scout still shows the full list, it just never auto-applies to it.
    """
    org_norm = _normalize_org(entry.get("company") or "")
    if not org_norm:
        return None
    for opt in entry.get("apply_options") or []:
        if not isinstance(opt, dict):
            continue
        url = (opt.get("url") or "").strip()
        if not _adapter_claimed(url):
            continue
        parsed = urlsplit(url)
        if org_norm in _normalize_org(parsed.netloc + parsed.path):
            return url
    return None


# Aggregator hosts whose posting pages expose <posting-url>/apply as a redirect
# to the employer's REAL ATS. Measured 2026-09-16 against the live parked
# queue: the arbeitnow page itself carries no employer ATS link in its HTML
# (resolve_from_page finds nothing), but that /apply path 302s straight to it.
# Of eight parked aggregator jobs the redirects landed on ashby (Mistral.ai),
# job-boards.eu.greenhouse.io (Yld), personio, recruitee x2 and join.com x2.
#
# Deliberately a short allowlist: /apply is an arbeitnow convention, not a
# universal one, and probing it on every feed URL would mean an unsolicited
# request to a stranger's site on every scan.
_APPLY_REDIRECT_HOSTS = ("arbeitnow.com", "arbeitnow.co.uk", "arbeitnow.fr")


class _Redirected(Exception):
    """The redirect we asked NOT to follow, carrying its target."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(url)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stops at the first hop: the Location IS the answer, and following it
    would fetch a third party's application page for no reason."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _Redirected(newurl)


def _apply_redirect_target(url: str) -> str:
    """Where <url>/apply redirects to, or "" on any failure -- a dead
    aggregator page must leave the job exactly as it was."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(url.rstrip("/") + "/apply",
                                           headers={"User-Agent": _UA}), timeout=_TIMEOUT)
    except _Redirected as hop:
        return hop.url or ""
    except Exception as exc:  # noqa: BLE001
        print(f"[ats-resolve] apply redirect failed: {type(exc).__name__}", file=sys.stderr)
    return ""


def resolve_from_apply_redirect(entry: dict) -> str | None:
    """Follow a known aggregator's /apply hop to the employer's own ATS.

    The same two guards as every other strategy: an adapter must claim the
    target's host (otherwise the portal runner gets a site it cannot fill and
    the job should stay needs_human), and the posting must belong to THIS
    company -- the EnthuZiastic/Cisco cross-listing rule applies to redirects
    just as much as to links on a page.
    """
    org_norm = _normalize_org(entry.get("company") or "")
    url = (entry.get("apply_target") or entry.get("url") or "").strip()
    if not org_norm or not url:
        return None
    host = safe_hostname(url)
    if not any(host_matches(host, allowed) for allowed in _APPLY_REDIRECT_HOSTS):
        return None

    target = _apply_redirect_target(url)
    if not target or not _adapter_claimed(target):
        return None
    parts = urlsplit(target)
    if org_norm not in _normalize_org(parts.netloc + parts.path):
        return None
    return target


def resolve_ats_url(entry: dict) -> str | None:
    """The full resolution for one queue entry. None = leave the job unchanged."""
    company = (entry.get("company") or "").strip()
    title = (entry.get("title") or "").strip()
    if not company or not title:
        return None

    hit = resolve_from_apply_options(entry)
    if hit:
        print(f"[ats-resolve] apply option: {hit}", file=sys.stderr)
        return hit

    url = (entry.get("apply_target") or entry.get("url") or "").strip()
    if url:
        hit = resolve_from_page(_fetch_page(url), company)
        if hit:
            print(f"[ats-resolve] page link: {hit}", file=sys.stderr)
            return hit
    hit = resolve_from_apply_redirect(entry)
    if hit:
        print(f"[ats-resolve] apply redirect: {hit}", file=sys.stderr)
        return hit

    hit = resolve_from_boards(company, title)
    if hit:
        print(f"[ats-resolve] board probe: {hit}", file=sys.stderr)
    return hit
