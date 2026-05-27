"""Fetch open jobs from external job sources (Ashby today; more later)."""
from __future__ import annotations
import re
import urllib.request
import json
from dataclasses import dataclass


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


def fetch_all(sources: list[dict]) -> list[JobPosting]:
    """sources = [{'kind': 'ashby', 'slug': 'xbowcareers', 'name': 'XBow'}, ...]"""
    out: list[JobPosting] = []
    for s in sources:
        if s["kind"] == "ashby":
            try:
                out.extend(fetch_ashby_org(s["slug"], s.get("name")))
            except Exception as e:
                print(f"warning: fetch failed for {s.get('name', s['slug'])}: {e}")
    return out
