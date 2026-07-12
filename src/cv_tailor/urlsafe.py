"""Browser-faithful hostname extraction for host-allowlist decisions.

`urllib.parse.urlparse` and a real browser (Chromium/WHATWG) disagree on a few
characters, and that gap is exploitable when the parsed host gates whether the
autonomous browser navigates to and fills PII into a page:

- backslash `\\` is an authority terminator for special schemes in the browser
  (treated as `/`), so `https://evil.com\\.jobs.ashbyhq.com/...` navigates to
  `evil.com` while urlparse reports `evil.com\\.jobs.ashbyhq.com` as the host;
- tab/newline/CR are stripped from the URL by the browser before parsing.

So the allowlist must not trust `urlparse().hostname` on any URL carrying those
characters. `safe_hostname` returns "" (caller degrades to no-match / needs_human)
for any URL that contains a parser-differential char or whose parsed host is not
a plain DNS name; otherwise the lowercased hostname.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# chars a browser strips or reinterprets in the authority -> never trust urlparse
_DIFFERENTIAL_CHARS = ("\\", "\t", "\n", "\r")
# a plain DNS hostname: labels of [a-z0-9-] joined by dots (also allows a
# trailing-dot FQDN, which fails the allowlist suffix check safely anyway)
_DNS_HOSTNAME_RE = re.compile(r"^[a-z0-9.\-]+$")


def safe_hostname(url: str) -> str:
    """Lowercased hostname of `url`, or "" when the URL is not safe to trust for
    a host-allowlist decision (parser-differential char present, or the parsed
    host is not a plain DNS name)."""
    if any(c in url for c in _DIFFERENTIAL_CHARS):
        return ""
    host = (urlparse(url).hostname or "").lower()
    if not host or not _DNS_HOSTNAME_RE.match(host):
        return ""
    return host


def host_matches(host: str, allowed: str) -> bool:
    """True when `host` IS `allowed` or a subdomain of it. `host` must already
    be a safe_hostname() result (empty never matches)."""
    return bool(host) and (host == allowed or host.endswith("." + allowed))
