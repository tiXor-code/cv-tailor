"""Fetch open jobs from external job sources (Ashby, Greenhouse, Lever, SerpAPI,
plus the free remote-job boards: Remotive, RemoteOK, Jobicy, WWR)."""
from __future__ import annotations
import html
import os
import re
import urllib.request
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlparse, quote_plus


@dataclass
class JobPosting:
    source: str       # "ashby"
    org: str          # "XBow", "Constructor", "Deel"
    title: str        # "Software Engineer - AI Systems"
    location: str     # "Europe (Remote)" etc.
    url: str          # public job URL
    description: str  # plain text, HTML stripped
    raw_id: str       # source-specific id for dedupe


HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}


def _strip_html(html: str) -> str:
    """Quick-and-dirty HTML to text. Good enough for LLM ingestion."""
    text = HTML_TAG_RE.sub(" ", html or "")
    for k, v in HTML_ENTITIES.items():
        text = text.replace(k, v)
    return re.sub(r"\s+", " ", text).strip()


def fetch_ashby_org(org_slug: str, display_name: str | None = None) -> list[JobPosting]:
    """Fetch all open jobs for one Ashby-hosted org. Raises on network/HTTP errors."""
    url = f"https://api.ashbyhq.com/posting-api/job-board/{org_slug}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "cv-tailor/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    name = display_name or org_slug
    out: list[JobPosting] = []
    for j in data.get("jobs", []):
        loc = j.get("location") or ""
        secondaries = j.get("secondaryLocations") or []
        if secondaries:
            loc += " · " + ", ".join(s.get("location", "") for s in secondaries if isinstance(s, dict))
        out.append(JobPosting(
            source="ashby",
            org=name,
            title=j.get("title") or "",
            location=loc,
            url=j.get("jobUrl") or "",
            description=_strip_html(j.get("descriptionHtml", "")),
            raw_id=j.get("id") or "",
        ))
    return out


def _http_json(url: str):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "cv-tailor/0.2"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def fetch_greenhouse_org(org_slug: str, display_name: str | None = None) -> list[JobPosting]:
    """Greenhouse public board API. content is HTML-entity-encoded -> unescape then strip."""
    data = _http_json(f"https://boards-api.greenhouse.io/v1/boards/{org_slug}/jobs?content=true")
    name = display_name or org_slug
    out: list[JobPosting] = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") if isinstance(j.get("location"), dict) else ""
        out.append(JobPosting(
            source="greenhouse", org=name, title=j.get("title") or "",
            location=loc, url=j.get("absolute_url") or "",
            description=_strip_html(html.unescape(j.get("content") or "")),
            raw_id=str(j.get("id") or ""),
        ))
    return out


def fetch_lever_org(org_slug: str, display_name: str | None = None) -> list[JobPosting]:
    """Lever public postings API (returns a JSON list)."""
    data = _http_json(f"https://api.lever.co/v0/postings/{org_slug}?mode=json")
    name = display_name or org_slug
    out: list[JobPosting] = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        desc = j.get("descriptionPlain") or _strip_html(j.get("description") or "")
        out.append(JobPosting(
            source="lever", org=name, title=j.get("text") or "",
            location=cats.get("location") or "", url=j.get("hostedUrl") or "",
            description=desc, raw_id=j.get("id") or "",
        ))
    return out


_SERP_JOB_BOARDS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
                    "google.com", "serpapi.com")

# ATS platforms whose subdomain or path segment usually carries the org's
# slug (e.g. "cisco.wd5.myworkdayjobs.com", "jobs.lever.co/acme/...").
_ATS_HOSTS = ("myworkdayjobs.com", "greenhouse.io", "lever.co", "ashbyhq.com",
              "smartrecruiters.com")

# Trailing legal-entity suffix stripped before alnum-normalizing an org name,
# so "Acme Inc" matches an ATS slug like "acme" the same way a bare-word org
# like "EnthuZiastic" matches "enthuziastic.com" with no stripping needed.
_ORG_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(inc\.?|incorporated|llc|ltd\.?|limited|corp\.?|corporation|co\.?|company|"
    r"gmbh|ag|s\.?a\.?|s\.?r\.?l\.?|plc|bv|pte\.?|pvt\.?)\s*$", re.I)


def _normalize_org(name: str) -> str:
    """Lowercase alnum-only org name for apply-link matching: 'EnthuZiastic'
    -> 'enthuziastic', 'Acme Inc' -> 'acme' (trailing legal suffix dropped
    first so it doesn't have to appear verbatim in a URL/slug)."""
    name = _ORG_LEGAL_SUFFIX_RE.sub("", (name or "").strip())
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _host_is(host: str, domain: str) -> bool:
    """True when `host` IS `domain` or a subdomain of it -- never a lookalike
    that merely embeds the name (boards.greenhouse.io.evil.com, acmegoogle.com).
    apply_options links are externally supplied, so board/ATS recognition must
    not be a substring check."""
    return host == domain or host.endswith("." + domain)


def _best_company_url(org, apply_options, share_link):
    """Rank apply links by how confidently they belong to `org`, so a
    cross-listed apply_options link never gets picked blindly. Real incident:
    a SerpAPI "EnthuZiastic - Generative AI Automation Engineer - Remote"
    card's only non-board apply_options link actually pointed at Cisco's
    Workday page (a different company, hybrid, US-onsite) -- see MEMORY.md.

    Preference order:
    1. A non-board link whose registrable domain contains the normalized org
       name -- likeliest the company's own careers page.
    2. An ATS-hosted link (Workday/Greenhouse/Lever/Ashby/SmartRecruiters)
       whose URL (subdomain or path) contains the normalized org name.
    3. Otherwise: share_link -- the Google Jobs page listing every apply
       option -- never a link that plainly names a different company.
    """
    org_norm = _normalize_org(org)
    links = [((opt or {}).get("link") or "") for opt in (apply_options or [])]
    links = [link for link in links if link]

    if org_norm:
        for link in links:
            host = (urlparse(link).hostname or "").lower()
            if any(_host_is(host, b) for b in _SERP_JOB_BOARDS):
                continue
            if org_norm in _normalize_org(host):
                return link

        for link in links:
            host = (urlparse(link).hostname or "").lower()
            if not any(_host_is(host, a) for a in _ATS_HOSTS):
                continue
            parsed = urlparse(link)
            if org_norm in _normalize_org(parsed.netloc + parsed.path):
                return link

    return share_link or ""


def fetch_serpapi(query: str, location: str | None = None, api_key: str | None = None,
                  hl: str = "en", budget=None) -> list[JobPosting]:
    """Google Jobs via SerpAPI. Remote-only (ltype=1). One page (~10 results).

    The API key is checked FIRST, before the budget is touched at all: a
    missing/unset SERPAPI_API_KEY returns [] without ever calling
    budget.take(), so a misconfigured key can never burn the shared monthly
    budget on queries that were never going to reach the network anyway
    (same failure class as this repo's Azure-key twice-bitten history --
    see MEMORY.md cv_tailor_azure_key_dead_scout_silent).

    `budget`, when given, is a budget.SerpBudget consulted AFTER the key
    check, BEFORE the network call. A spent budget blocks the query and
    returns [] without hitting the network -- loudly logged so a
    silently-empty scan is never mistaken for "no good jobs today".
    `budget=None` (the default) preserves old unlimited/unbudgeted behavior,
    so existing callers are unaffected."""
    api_key = api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("warning: SERPAPI_API_KEY not set; skipping serpapi source")
        return []
    if budget is not None and not budget.take():
        print(f"serpapi budget exhausted (cap {budget.monthly_cap}/mo, "
              f"{budget.used()} used this month); skipping remaining queries "
              f"-- dropped {query!r}")
        return []
    params = [f"engine=google_jobs", f"q={quote_plus(query)}", f"hl={hl}", "ltype=1",
              f"api_key={api_key}"]
    if location:
        params.append(f"location={quote_plus(location)}")
    data = _http_json("https://serpapi.com/search?" + "&".join(params))
    out: list[JobPosting] = []
    for j in data.get("jobs_results", []):
        org = j.get("company_name") or ""
        out.append(JobPosting(
            source="serpapi", org=org,
            title=j.get("title") or "", location=j.get("location") or "",
            url=_best_company_url(org, j.get("apply_options"), j.get("share_link")),
            description=j.get("description") or "", raw_id=j.get("job_id") or "",
        ))
    return out


# Free remote-job boards (no API key). Each fetcher takes one filter param so
# fetch_all can call it directly from a sources.yaml entry, mirroring the
# ashby/greenhouse/lever (slug, name) dispatch shape below. Every fetcher
# catches its own errors -- warns and returns [] -- so one dead board never
# kills the scan (ported from norina-jobs/src/norina/boards.py).
_BOARD_UA = "cv-tailor/0.3 (job scan; contact@teodorlutoiu.com)"


def fetch_remotive(category: str) -> list[JobPosting]:
    """Remotive public API, one category at a time (e.g. 'software-dev', 'marketing')."""
    try:
        data = _http_json(f"https://remotive.com/api/remote-jobs?category={quote_plus(category)}")
    except Exception as e:
        print(f"warning: remotive fetch failed for category={category!r}: {e}")
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs", []):
        out.append(JobPosting(
            source="remotive", org=j.get("company_name") or "",
            title=j.get("title") or "",
            location=f"Remote - {j.get('candidate_required_location') or 'Anywhere'}",
            url=j.get("url") or "", description=_strip_html(j.get("description") or ""),
            raw_id=str(j.get("id") or j.get("url") or ""),
        ))
    return out


def fetch_remoteok(tag: str) -> list[JobPosting]:
    """RemoteOK public API filtered by a single tag. Element 0 of the response is
    always a legal notice, not a job -- skip it."""
    try:
        req = urllib.request.Request(
            f"https://remoteok.com/api?tags={quote_plus(tag)}",
            headers={"Accept": "application/json", "User-Agent": _BOARD_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"warning: remoteok fetch failed for tag={tag!r}: {e}")
        return []
    out: list[JobPosting] = []
    for j in data[1:] if isinstance(data, list) else []:
        out.append(JobPosting(
            source="remoteok", org=j.get("company") or "",
            title=j.get("position") or "",
            location=f"Remote - {j.get('location') or 'Anywhere'}",
            url=j.get("url") or "", description=_strip_html(j.get("description") or ""),
            raw_id=str(j.get("id") or ""),
        ))
    return out


def fetch_jobicy(count: int, tag: str) -> list[JobPosting]:
    """Jobicy public API filtered by tag, capped at count results."""
    try:
        data = _http_json(
            f"https://jobicy.com/api/v2/remote-jobs?count={count}&tag={quote_plus(tag)}")
    except Exception as e:
        print(f"warning: jobicy fetch failed for tag={tag!r}: {e}")
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs", []):
        out.append(JobPosting(
            source="jobicy", org=j.get("companyName") or "",
            title=j.get("jobTitle") or "",
            location=f"Remote - {j.get('jobGeo') or 'Anywhere'}",
            url=j.get("url") or "", description=_strip_html(j.get("jobDescription") or ""),
            raw_id=str(j.get("id") or ""),
        ))
    return out


def _parse_wwr_rss(root: ET.Element) -> list[JobPosting]:
    out: list[JobPosting] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        org, _, role = title.partition(":")
        if not role:
            org, role = "", title
        region = (item.findtext("region") or "").strip()
        out.append(JobPosting(
            source="wwr", org=org.strip(), title=role.strip(),
            location=f"Remote - {region or 'Anywhere'}",
            url=(item.findtext("link") or "").strip(),
            description=_strip_html(item.findtext("description") or ""),
            raw_id=(item.findtext("guid") or item.findtext("link") or "").strip(),
        ))
    return out


def fetch_wwr(category: str) -> list[JobPosting]:
    """We Work Remotely RSS feed for one category slug (e.g. 'programming',
    'sales-and-marketing')."""
    url = f"https://weworkremotely.com/categories/remote-{category}-jobs.rss"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BOARD_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"warning: wwr fetch failed for category={category!r}: {e}")
        return []
    return _parse_wwr_rss(root)


def fetch_all(sources: list[dict], serp_budget=None) -> list[JobPosting]:
    """sources = [{'kind': 'ashby'|'greenhouse'|'lever', 'slug': '...', 'name': '...'}, ...]

    `serp_budget`, when given (a budget.SerpBudget), is the ONE shared
    instance threaded through every serpapi source below, so the counter
    reflects queries actually taken across the whole scan rather than each
    source re-reading a stale file. `serp_budget=None` (the default) means
    unbudgeted -- every serpapi source fires -- matching pre-budget behavior
    for callers (including existing tests) that do not pass one."""
    dispatch = {
        "ashby": fetch_ashby_org,
        "greenhouse": fetch_greenhouse_org,
        "lever": fetch_lever_org,
    }
    board_dispatch = {
        "remotive": lambda s: fetch_remotive(s["category"]),
        "remoteok": lambda s: fetch_remoteok(s["tag"]),
        "jobicy": lambda s: fetch_jobicy(s.get("count", 50), s["tag"]),
        "wwr": lambda s: fetch_wwr(s["category"]),
    }
    out: list[JobPosting] = []
    for s in sources:
        kind = s["kind"]
        if kind == "serpapi":
            try:
                out.extend(fetch_serpapi(s["query"], s.get("location"), budget=serp_budget))
            except Exception as e:
                print(f"warning: serpapi fetch failed for {s.get('query')!r}: {e}")
            continue
        if kind in board_dispatch:
            try:
                out.extend(board_dispatch[kind](s))
            except Exception as e:
                print(f"warning: {kind} fetch failed (bad source config?): {e}")
            continue
        fn = dispatch.get(kind)
        if not fn:
            print(f"warning: unknown source kind {kind!r}; skipping")
            continue
        try:
            out.extend(fn(s["slug"], s.get("name")))
        except Exception as e:
            print(f"warning: fetch failed for {s.get('name', s['slug'])}: {e}")
    return out
