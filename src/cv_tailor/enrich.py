# src/cv_tailor/enrich.py
"""Gate 2: SMB (startup/scaleup) detection.

Phase 1 = provenance only: which board/ATS the posting came from is a strong,
free signal. Startup ATSs and remote-startup boards => SMB-likely; enterprise
HRIS => drop. Phase 2 adds a cached Hunter headcount lookup for ambiguous
aggregator (serpapi) hits."""
from __future__ import annotations
import re
import json
import os
import urllib.request
from urllib.parse import urlparse
from cv_tailor.cache import get_enrichment, put_enrichment

JOB_BOARD_DOMAINS = {
    "greenhouse.io", "boards.greenhouse.io", "lever.co", "jobs.lever.co",
    "ashbyhq.com", "jobs.ashbyhq.com", "workable.com", "linkedin.com",
    "indeed.com", "glassdoor.com", "google.com", "ziprecruiter.com",
    "remotive.com", "remoteok.com", "weworkremotely.com", "himalayas.app",
    "wellfound.com", "builtin.com", "smartrecruiters.com",
}
SMB_EMPLOYEE_CEILING = 500  # startups & scaleups up to ~500


def _registrable(host: str) -> str:
    host = (host or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def company_domain(job):
    """Best-effort company domain from the posting URL; None for job-board URLs."""
    host = _registrable(urlparse(job.url or "").netloc)
    if not host:
        return None
    if host in JOB_BOARD_DOMAINS or any(host.endswith("." + b) for b in JOB_BOARD_DOMAINS):
        return None
    return host


def _parse_count(token):
    """Parse one Hunter count token to an int. Handles K/M suffixes:
    '250'->250, '1K'->1000, '10K'->10000, '100K+'->100000, '1M'->1000000.
    Returns None if the token has no number."""
    t = token.strip().upper().replace("+", "").replace(",", "")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([KM]?)$", t)
    if not m:
        return None
    val = float(m.group(1))
    mult = {"K": 1_000, "M": 1_000_000, "": 1}[m.group(2)]
    return int(val * mult)


def classify_headcount(employees):
    """Hunter range string -> is_smb (True/False), or None if unknown/unparseable.

    Hunter returns ranges like '51-250', '251-1K', '10K-50K', '100K+'. We classify
    SMB on the LOWER bound vs the ceiling (recall-favoring: keep borderline scaleups,
    drop only clear enterprises; the LLM scorer is the final arbiter)."""
    if not employees:
        return None
    nums = [_parse_count(tok) for tok in re.split(r"[-–—to/]+", employees.strip(), flags=re.I)]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None
    return min(nums) <= SMB_EMPLOYEE_CEILING


# Boards/ATS used overwhelmingly by startups & scaleups.
STARTUP_ATS = {"ashby", "greenhouse", "lever", "workable"}
# Remote-job boards: startup-skewed but mixed; provenance is weaker (Phase 2 refines).
REMOTE_BOARDS = {"remotive", "remoteok", "wwr", "himalayas"}
# Enterprise HRIS — almost never SMB.
ENTERPRISE_HRIS = {"workday", "successfactors", "taleo", "icims", "brassring", "smartrecruiters"}


def hunter_headcount(domain, api_key=None):
    """Return Hunter's employee-range string for a domain, or None on any failure."""
    api_key = api_key or os.environ.get("HUNTER_API_KEY")
    if not api_key:
        return None
    url = f"https://api.hunter.io/v2/companies/find?domain={domain}&api_key={api_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cv-tailor/0.2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return ((data or {}).get("data") or {}).get("metrics", {}).get("employees")
    except Exception:
        return None


def _hunter_verdict(job, conn):
    """SMB verdict for an ambiguous (serpapi/unknown) source via cached Hunter lookup.
    Returns True/False, or None if undeterminable (caller falls back to pass)."""
    domain = company_domain(job)
    if not domain:
        return None
    cached = get_enrichment(conn, domain)
    if cached is not None:
        return cached["is_smb"]
    headcount = hunter_headcount(domain)
    verdict = classify_headcount(headcount)
    if verdict is None:
        return None
    put_enrichment(conn, domain, is_smb=verdict, headcount=headcount, signal="hunter")
    return verdict


def is_smb(job, conn=None):
    src = (job.source or "").lower()
    if src in ENTERPRISE_HRIS:
        return False
    if src in STARTUP_ATS or src in REMOTE_BOARDS:
        return True
    # Ambiguous (serpapi / unknown): use Hunter when a cache+conn is available.
    if conn is not None:
        v = _hunter_verdict(job, conn)
        if v is not None:
            return v
    return True  # undeterminable -> pass; the LLM scorer is the final arbiter


def smb_hint(job, conn=None):
    src = (job.source or "").lower()
    if src in STARTUP_ATS:
        return "startup/scaleup (startup ATS)"
    if src in REMOTE_BOARDS:
        return "likely startup/scaleup (remote board)"
    if src in ENTERPRISE_HRIS:
        return "enterprise (enterprise HRIS)"
    if conn is not None:
        domain = company_domain(job)
        if domain:
            cached = get_enrichment(conn, domain)
            if cached and cached.get("headcount"):
                return f"~{cached['headcount']} employees (Hunter)"
    return "unknown size"
