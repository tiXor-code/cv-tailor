"""Adopt a job Teodor opened himself into his Scout list (Scout Fill, 2026-09-28).

He clicks the Scout icon on an application form for a job Scout never found
(join.com, a company careers page). Before filling anything, Scout needs the
job description so the CV, cover letter and answers are tailored to it. This
module finds that JD:

1. the schema.org JobPosting (JSON-LD) on the page he is on, or on its parent
   pages (join.com's .../<id>/apply/cv form sits under the posting at .../<id>);
2. otherwise the page text the extension read, when it is long enough to be a
   JD.

The URL comes from a web page, so fetching it is guarded: https only, public
addresses only (checked on every redirect hop), a size cap and a timeout.
"""
from __future__ import annotations

import hashlib
import html as _html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse, urlunparse

from cv_tailor.job_sources import _strip_html
from cv_tailor.urlsafe import safe_hostname

MAX_BYTES = 3 * 1024 * 1024
MIN_PAGE_TEXT = 800          # shorter than this is a login wall or a bare form, not a JD
MAX_DESCRIPTION = 20_000
_LD_RE = re.compile(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.I | re.S)


class AdoptError(Exception):
    pass


def _public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            return False
    return bool(infos)


def _check_url(url: str) -> str:
    host = safe_hostname(url)
    if urlparse(url).scheme != "https" or not host or not _public_host(host):
        raise AdoptError(f"refusing to fetch {url[:200]!r}")
    return url


class _Guarded(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, opener=None) -> tuple[str, str]:
    """(final_url, html) of a public https page, or AdoptError."""
    _check_url(url)
    opener = opener or urllib.request.build_opener(_Guarded)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Scout Fill)", "Accept": "text/html"})
    try:
        with opener.open(req, timeout=15) as resp:
            body = resp.read(MAX_BYTES + 1)
            final = resp.geturl()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AdoptError(f"fetch failed: {type(exc).__name__}") from exc
    return final, body[:MAX_BYTES].decode("utf-8", "replace")


def candidate_urls(url: str) -> list[str]:
    """The page, then up to two parent pages, without query or fragment."""
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    out = []
    for cut in range(len(parts), max(len(parts) - 3, 1), -1):
        path = "/" + "/".join(parts[:cut])
        out.append(urlunparse((u.scheme, u.netloc, path, "", "", "")))
    return out or [urlunparse((u.scheme, u.netloc, u.path or "/", "", "", ""))]


def _postings(node):
    if isinstance(node, list):
        for n in node:
            yield from _postings(n)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _postings(node[key])


def _text(value) -> str:
    return _strip_html(_html.unescape(str(value or "")))


def _location(p: dict) -> str:
    bits = []
    if p.get("jobLocationType") == "TELECOMMUTE":
        bits.append("Remote")
    for key in ("applicantLocationRequirements", "jobLocation"):
        for loc in p.get(key) if isinstance(p.get(key), list) else [p.get(key)]:
            if isinstance(loc, dict):
                addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
                name = loc.get("name") or ", ".join(
                    str(addr.get(k)) for k in ("addressLocality", "addressCountry") if addr.get(k))
                if name:
                    bits.append(str(name))
    return " - ".join(dict.fromkeys(bits))


def posting_from_html(page_html: str, page_url: str) -> dict | None:
    for block in _LD_RE.findall(page_html or ""):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for p in _postings(data):
            desc = _text(p.get("description"))[:MAX_DESCRIPTION]
            org = p.get("hiringOrganization")
            company = org.get("name") if isinstance(org, dict) else org
            if len(desc) < 200 or not p.get("title"):
                continue
            return {"title": _text(p.get("title")), "company": _text(company) or "",
                    "location": _location(p), "url": str(p.get("url") or page_url),
                    "description": desc}
    return None


def _no_fetch(host: str) -> bool:
    # Teodor: no custom LinkedIn scraper. On LinkedIn the description comes
    # only from the page he has open in his own browser (page_text).
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def find_posting(url: str, page_title: str = "", page_text: str = "", company: str = "", *,
                 fetcher=fetch) -> dict:
    """The job's posting: JSON-LD from the page or its parents, else his page's text."""
    for cand in ([] if _no_fetch(safe_hostname(url)) else candidate_urls(url)):
        try:
            final, body = fetcher(cand)
        except AdoptError:
            continue
        found = posting_from_html(body, final)
        if found:
            return found
    text = re.sub(r"\s+", " ", page_text or "").strip()
    if len(text) >= MIN_PAGE_TEXT:
        host = safe_hostname(url)
        return {"title": (page_title or "").split("|")[0].strip()[:200] or "Unknown role",
                "company": (company or "").strip()[:200] or (host.split(".")[-2] if host.count(".") >= 1 else host),
                "location": "", "url": url.split("#")[0], "description": text[:MAX_DESCRIPTION]}
    raise AdoptError("no job description found on this page or its parent pages")


def job_id_for(posting_url: str) -> str:
    u = urlparse(posting_url)
    basis = f"extension:{u.netloc.lower()}{u.path.rstrip('/')}"
    return hashlib.sha1(basis.encode()).hexdigest()[:16]
