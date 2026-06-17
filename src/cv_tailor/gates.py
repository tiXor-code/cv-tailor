# src/cv_tailor/gates.py
"""Gate 1: free rule-based pre-filter (remote / EU-eligible / target keyword).
Heuristic by design — the LLM scorer is the final arbiter. Goal is to cheaply
drop the obvious-no's before any paid enrichment or scoring call."""
from __future__ import annotations
import re

_REMOTE_RE = re.compile(r"\bremote\b|work from home|\bwfh\b|distributed|work from anywhere", re.I)
_GLOBAL_RE = re.compile(r"\bglobal(ly)?\b|\bworldwide\b|\banywhere\b|\bemea\b", re.I)
_EU_RE = re.compile(
    r"\b(eu|europe|european|cet|cest|gmt|uk|united kingdom|ireland|germany|france|spain|"
    r"portugal|netherlands|belgium|poland|romania|bulgaria|italy|austria|switzerland|"
    r"sweden|norway|denmark|finland|estonia|lithuania|latvia|czech|greece|hungary)\b", re.I)
# US-only / non-EU exclusions that override a generic "remote".
_US_ONLY_RE = re.compile(r"us[- ]only|u\.s\.[- ]only|must be (us|united states)[- ]based|"
                         r"us work authorization|gc/?citizen", re.I)


def _blob(location: str, description: str, cap: int = 2000) -> str:
    return f"{location or ''} {(description or '')[:cap]}"


def is_remote(location: str, description: str) -> bool:
    text = _blob(location, description)
    if _REMOTE_RE.search(text):
        return True
    return False


def is_eu_eligible(location: str, description: str) -> bool:
    text = _blob(location, description)
    if _US_ONLY_RE.search(text):
        return False
    if _GLOBAL_RE.search(text):
        return True
    return bool(_EU_RE.search(text))


def has_target_keyword(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


def passes_gate1(job, keywords: list[str]) -> bool:
    if not is_remote(job.location, job.description):
        return False
    if not is_eu_eligible(job.location, job.description):
        return False
    return has_target_keyword(f"{job.title} {job.description}", keywords)
