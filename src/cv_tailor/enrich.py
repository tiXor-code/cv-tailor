# src/cv_tailor/enrich.py
"""Gate 2: SMB (startup/scaleup) detection.

Phase 1 = provenance only: which board/ATS the posting came from is a strong,
free signal. Startup ATSs and remote-startup boards => SMB-likely; enterprise
HRIS => drop. Phase 2 adds a cached Hunter headcount lookup for ambiguous
aggregator (serpapi) hits."""
from __future__ import annotations

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
