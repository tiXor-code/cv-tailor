"""Fetch open jobs from external job sources (Ashby today; more later)."""
from __future__ import annotations
import html
import os
import re
import urllib.request
import json
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


def _best_company_url(apply_options, share_link):
    """Prefer an apply link that is NOT a big job board (likelier a company domain),
    so Hunter can resolve a real company domain later. Fall back to share_link."""
    for opt in apply_options or []:
        link = (opt or {}).get("link") or ""
        host = urlparse(link).netloc.lower()
        if link and not any(b in host for b in _SERP_JOB_BOARDS):
            return link
    if apply_options and apply_options[0].get("link"):
        return apply_options[0]["link"]
    return share_link or ""


def fetch_serpapi(query: str, location: str | None = None, api_key: str | None = None,
                  hl: str = "en") -> list[JobPosting]:
    """Google Jobs via SerpAPI. Remote-only (ltype=1). One page (~10 results)."""
    api_key = api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("warning: SERPAPI_API_KEY not set; skipping serpapi source")
        return []
    params = [f"engine=google_jobs", f"q={quote_plus(query)}", f"hl={hl}", "ltype=1",
              f"api_key={api_key}"]
    if location:
        params.append(f"location={quote_plus(location)}")
    data = _http_json("https://serpapi.com/search?" + "&".join(params))
    out: list[JobPosting] = []
    for j in data.get("jobs_results", []):
        out.append(JobPosting(
            source="serpapi", org=j.get("company_name") or "",
            title=j.get("title") or "", location=j.get("location") or "",
            url=_best_company_url(j.get("apply_options"), j.get("share_link")),
            description=j.get("description") or "", raw_id=j.get("job_id") or "",
        ))
    return out


def fetch_all(sources: list[dict]) -> list[JobPosting]:
    """sources = [{'kind': 'ashby'|'greenhouse'|'lever', 'slug': '...', 'name': '...'}, ...]"""
    dispatch = {
        "ashby": fetch_ashby_org,
        "greenhouse": fetch_greenhouse_org,
        "lever": fetch_lever_org,
    }
    out: list[JobPosting] = []
    for s in sources:
        if s["kind"] == "serpapi":
            try:
                out.extend(fetch_serpapi(s["query"], s.get("location")))
            except Exception as e:
                print(f"warning: serpapi fetch failed for {s.get('query')!r}: {e}")
            continue
        fn = dispatch.get(s["kind"])
        if not fn:
            print(f"warning: unknown source kind {s['kind']!r}; skipping")
            continue
        try:
            out.extend(fn(s["slug"], s.get("name")))
        except Exception as e:
            print(f"warning: fetch failed for {s.get('name', s['slug'])}: {e}")
    return out
