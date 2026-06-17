# src/cv_tailor/enrich.py
"""Gate 2: SMB (startup/scaleup) detection.

Phase 1 = provenance only: which board/ATS the posting came from is a strong,
free signal. Startup ATSs and remote-startup boards => SMB-likely; enterprise
HRIS => drop. Phase 2 adds a cached Hunter headcount lookup for ambiguous
aggregator (serpapi) hits."""
from __future__ import annotations
import re
from urllib.parse import urlparse

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


def classify_headcount(employees):
    """Hunter range string -> is_smb (True/False), or None if unknown/unparseable."""
    if not employees:
        return None
    upper = re.findall(r"\d+", employees.replace(",", ""))
    if not upper:
        return None
    top = int(upper[-1])  # use the upper bound of the range ("201-500" -> 500)
    return top <= SMB_EMPLOYEE_CEILING


# Boards/ATS used overwhelmingly by startups & scaleups.
STARTUP_ATS = {"ashby", "greenhouse", "lever", "workable"}
# Remote-job boards: startup-skewed but mixed; provenance is weaker (Phase 2 refines).
REMOTE_BOARDS = {"remotive", "remoteok", "wwr", "himalayas"}
# Enterprise HRIS — almost never SMB.
ENTERPRISE_HRIS = {"workday", "successfactors", "taleo", "icims", "brassring", "smartrecruiters"}


def is_smb(job) -> bool:
    src = (job.source or "").lower()
    if src in ENTERPRISE_HRIS:
        return False
    if src in STARTUP_ATS or src in REMOTE_BOARDS:
        return True
    # Unknown provenance (e.g. serpapi in Phase 1) passes; Phase 2 Hunter refines.
    return True


def smb_hint(job) -> str:
    """A short company-size hint string to feed the LLM scorer."""
    src = (job.source or "").lower()
    if src in STARTUP_ATS:
        return "startup/scaleup (startup ATS)"
    if src in REMOTE_BOARDS:
        return "likely startup/scaleup (remote board)"
    if src in ENTERPRISE_HRIS:
        return "enterprise (enterprise HRIS)"
    return "unknown size"
