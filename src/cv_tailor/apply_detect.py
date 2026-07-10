"""Detect whether a job is applied to by email or portal, from the JD text."""
from __future__ import annotations
import re

_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
# An email only counts when it appears in an application-instruction context.
_PATTERNS = [
    re.compile(r"mailto:(" + _EMAIL + ")", re.I),
    re.compile(r"(?:send|email|submit|forward)\s+(?:your\s+|a\s+)?(?:cv|resume|résumé|application|portfolio)[^.\n]{0,40}?\bto\b\s*:?\s*(" + _EMAIL + ")", re.I),
    re.compile(r"applications?\s+(?:should\s+be\s+sent\s+)?to\s*:?\s*(" + _EMAIL + ")", re.I),
    re.compile(r"apply\s+(?:at|via|by\s+email(?:ing)?)\s*:?\s*(" + _EMAIL + ")", re.I),
]
# Broader pattern to match the entire "send...to..." clause including multiple emails
# Match until we hit a sentence-ending punctuation or double space (paragraph break)
_SEND_CLAUSE = re.compile(r"(?:send|email|submit|forward)\s+(?:your\s+|a\s+)?(?:cv|resume|résumé|application|portfolio)[^.\n]{0,40}?\bto\b(?:[^!\n](?!\s{2}))*", re.I)
_BLOCKED_LOCAL = ("noreply", "no-reply", "donotreply", "privacy", "gdpr", "support", "unsubscribe")


def detect_apply_channel(description, company_domain=None):
    text = description or ""
    found = []
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            addr = m.group(1).lower().rstrip(".")
            local = addr.split("@")[0]
            if any(b in local for b in _BLOCKED_LOCAL):
                continue
            if addr not in found:
                found.append(addr)

    # Also extract any additional emails from "send...to..." clauses
    # (e.g., "send to email1 or email2")
    for m in _SEND_CLAUSE.finditer(text):
        clause = m.group(0)
        # Extract all emails from the clause, beyond what the pattern captures
        for email_match in re.finditer(_EMAIL, clause):
            addr = email_match.group(0).lower().rstrip(".")
            local = addr.split("@")[0]
            if any(b in local for b in _BLOCKED_LOCAL):
                continue
            if addr not in found:
                found.append(addr)

    if not found:
        return ("portal", None)
    if company_domain:
        for addr in found:
            if addr.endswith("@" + company_domain.lower()):
                return ("email", addr)
    if len(found) == 1:
        return ("email", found[0])
    return ("portal", None)  # ambiguous
