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
    # Hacker News "Who is hiring" phrasings (2026-09-25: 146 of 256 September
    # posts took email; the four patterns above caught 8).
    re.compile(r"\bcontact\s*:\s*(" + _EMAIL + ")", re.I),
    re.compile(r"\bto\s+apply\b[,:]?\s*(?:(?:please\s+)?(?:email|contact|send\s+\w+\s+to|write\s+to|reach\s+out\s+to)\s*:?\s*)?(" + _EMAIL + ")", re.I),
    re.compile(r"\bhow\s+to\s+apply\s*:?\s*(?:email|contact)?\s*:?\s*(" + _EMAIL + ")", re.I),
    re.compile(r"\bapply\s*:\s*(" + _EMAIL + ")", re.I),
    re.compile(r"\bemail\s+(?:me|us)\b[^.\n@]{0,40}?\bat\s*:?\s*(" + _EMAIL + ")", re.I),
    re.compile(r"\bapply\s+with\b[^.\n@]{0,40}?:\s*(" + _EMAIL + ")", re.I),
    # "Email jobs@x.io with your CV" / "... email ada@x.io." -- never "reach out
    # to X": that phrasing is as often a questions address as an application
    # one (test_single_line_second_sentence_email_does_not_downgrade).
    re.compile(r"\bemail\s*:?\s*(" + _EMAIL + r")(?=\s+(?:with|and|if|to)\b|\s*[—\-]|[.,;!]?\s*$)", re.I),
]
# "jobs [at] acme [dot] com", "ada (at) acme (dot) io", "ada at acme dot io".
# Decoded before matching so the patterns above see a plain address. The
# bare-word form needs BOTH " at " and " dot " around word-only parts.
_OBFUSCATED = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+)\s*([A-Za-z0-9-]+)"
    r"\s*(?:\[dot\]|\(dot\)|\{dot\}|\s+dot\s+)\s*([A-Za-z]{2,})\b", re.I)


def _deobfuscate(text: str) -> str:
    return _OBFUSCATED.sub(lambda m: f"{m.group(1)}@{m.group(2)}.{m.group(3)}", text)
# After a trigger pattern captures its first email, additional emails joined
# immediately by "or" / "and" / "/" are also candidates (e.g. "send your CV
# to a@x.com or b@x.com"). Only horizontal whitespace is allowed before the
# joiner, so any sentence-ending punctuation (. ! ?) or newline between the
# previous email and the joiner breaks the chain -- it can never be consumed
# by this pattern, keeping the match bounded to the same sentence/line.
_OR_JOINED = re.compile(r"[ \t]*(?:,[ \t]*)?(?:(?:or|and)[ \t]+|/[ \t]*)(" + _EMAIL + ")", re.I)
_BLOCKED_LOCAL = ("noreply", "no-reply", "donotreply", "privacy", "gdpr", "support", "unsubscribe")


def _is_blocked(addr: str) -> bool:
    local = addr.split("@")[0]
    return any(b in local for b in _BLOCKED_LOCAL)


def detect_apply_channel(description: str, company_domain: str | None = None) -> tuple[str, str | None]:
    text = _deobfuscate(description or "")
    found: list[str] = []

    for pat in _PATTERNS:
        for m in pat.finditer(text):
            candidates = [m.group(1)]
            pos = m.end(1)
            while True:
                extra = _OR_JOINED.match(text, pos)
                if not extra:
                    break
                candidates.append(extra.group(1))
                pos = extra.end(1)
            for raw in candidates:
                addr = raw.lower().rstrip(".")
                if _is_blocked(addr):
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
