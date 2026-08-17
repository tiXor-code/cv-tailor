"""Fetch open jobs from external job sources (Ashby, Greenhouse, Lever, SerpAPI,
Adzuna, plus the free remote-job boards: Remotive, RemoteOK, Jobicy, WWR,
Arbeitnow, Himalayas).

Credentialed sources (SerpAPI, Adzuna) read their keys from the ENVIRONMENT
only -- never from a committed config file, and never from a sources.yaml
entry -- and each no-ops with a warning when its key is unset, so an
unconfigured source can never take a budget slot or reach the network."""
from __future__ import annotations
import html
import os
import re
import urllib.error
import urllib.request
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import urlparse, quote_plus, unquote_plus

from cv_tailor.gates import is_remote
from cv_tailor.urlsafe import host_matches, safe_hostname


@dataclass
class JobPosting:
    source: str       # "ashby"
    org: str          # "XBow", "Constructor", "Deel"
    title: str        # "Software Engineer - AI Systems"
    location: str     # "Europe (Remote)" etc.
    url: str          # public job URL
    description: str  # plain text, HTML stripped
    raw_id: str       # source-specific id for dedupe
    # Every apply link the source offered, normalized to {"label", "url"}.
    # `url` above is the ONE link chosen for automation (_best_company_url,
    # which deliberately falls back to a Google Jobs share_link when no link
    # provably belongs to the org). This list is everything that ranking saw,
    # kept so the ATS resolver gets a second chance and so a human is never
    # left with only the SERP link. Empty for sources that offer no such list.
    apply_options: list[dict] = field(default_factory=list)


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

# ATS platforms whose subdomain or path segment usually carries the org's
# slug (e.g. "cisco.wd5.myworkdayjobs.com", "jobs.lever.co/acme/...").
_ATS_HOSTS = ("myworkdayjobs.com", "greenhouse.io", "lever.co", "ashbyhq.com",
              "smartrecruiters.com")

# Trailing legal-entity suffix stripped before alnum-normalizing an org name,
# so "Acme Inc" matches an ATS slug like "acme" the same way a bare-word org
# like "EnthuZiastic" matches "enthuziastic.com" with no stripping needed.
_ORG_LEGAL_SUFFIX_RE = re.compile(
    r"\s+(inc\.?|incorporated|llc|ltd\.?|limited|corp\.?|corporation|co\.?|company|"
    r"gmbh|ag|s\.?a\.?|s\.?r\.?l\.?|plc|bv|pte\.?|pvt\.?)\s*$", re.I)


def _normalize_org(name: str) -> str:
    """Lowercase alnum-only org name for apply-link matching: 'EnthuZiastic'
    -> 'enthuziastic', 'Acme Inc' -> 'acme' (trailing legal suffix dropped
    first so it doesn't have to appear verbatim in a URL/slug)."""
    name = _ORG_LEGAL_SUFFIX_RE.sub("", (name or "").strip())
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _host_is(host: str, domain: str) -> bool:
    """True when `host` IS `domain` or a subdomain of it -- never a lookalike
    that merely embeds the name (boards.greenhouse.io.evil.com, acmegoogle.com).
    `host` must be a safe_hostname() result so a backslash/whitespace parser
    differential (which chains into adapter_for) can't slip through as a match."""
    return host_matches(host, domain)


def _best_company_url(org, apply_options, share_link):
    """Rank apply links by how confidently they belong to `org`, so a
    cross-listed apply_options link never gets picked blindly. Real incident:
    a SerpAPI "EnthuZiastic - Generative AI Automation Engineer - Remote"
    card's only non-board apply_options link actually pointed at Cisco's
    Workday page (a different company, hybrid, US-onsite) -- see MEMORY.md.

    Preference order:
    1. A non-board link whose registrable domain contains the normalized org
       name -- likeliest the company's own careers page.
    2. An ATS-hosted link (Workday/Greenhouse/Lever/Ashby/SmartRecruiters)
       whose URL (subdomain or path) contains the normalized org name.
    3. Otherwise: share_link -- the Google Jobs page listing every apply
       option -- never a link that plainly names a different company.
    """
    org_norm = _normalize_org(org)
    links = [((opt or {}).get("link") or "") for opt in (apply_options or [])]
    links = [link for link in links if link]

    if org_norm:
        for link in links:
            host = safe_hostname(link)
            if any(_host_is(host, b) for b in _SERP_JOB_BOARDS):
                continue
            if org_norm in _normalize_org(host):
                return link

        for link in links:
            host = safe_hostname(link)
            if not any(_host_is(host, a) for a in _ATS_HOSTS):
                continue
            parsed = urlparse(link)
            if org_norm in _normalize_org(parsed.netloc + parsed.path):
                return link

    return share_link or ""


# Upper bound on stored/rendered apply links per job. SerpAPI returns a
# handful; the cap keeps one pathological response from bloating every queue
# entry and the /scout card that renders them.
_MAX_APPLY_OPTIONS = 8


def _apply_option_links(apply_options) -> list[dict]:
    """Normalize a source's raw apply-option list to [{"label", "url"}, ...].

    Untrusted input: these links come straight from the SerpAPI response and
    end up both in an <a href> on /scout and in a host-allowlist decision in
    ats_resolve. So each one must survive safe_hostname (which returns "" for
    parser-differential hosts like `evil.com\\.jobs.ashbyhq.com` -- the
    1d9c700 fix) and be plain http(s). Anything else is dropped silently:
    a bad link is not an error, it just isn't offered.

    Order is preserved (SerpAPI ranks by relevance), duplicates collapse to
    their first occurrence, and the result is capped at _MAX_APPLY_OPTIONS.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for opt in apply_options or []:
        if not isinstance(opt, dict):
            continue
        url = (opt.get("link") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        host = safe_hostname(url)
        if not host or url in seen:
            continue
        seen.add(url)
        out.append({"label": (opt.get("title") or "").strip() or host, "url": url})
        if len(out) >= _MAX_APPLY_OPTIONS:
            break
    return out


def fetch_serpapi(query: str, location: str | None = None, api_key: str | None = None,
                  hl: str = "en", budget=None) -> list[JobPosting]:
    """Google Jobs via SerpAPI. Remote-only (ltype=1). One page (~10 results).

    The API key is checked FIRST, before the budget is touched at all: a
    missing/unset SERPAPI_API_KEY returns [] without ever calling
    budget.take(), so a misconfigured key can never burn the shared monthly
    budget on queries that were never going to reach the network anyway
    (same failure class as this repo's Azure-key twice-bitten history --
    see MEMORY.md cv_tailor_azure_key_dead_scout_silent).

    `budget`, when given, is a budget.SerpBudget consulted AFTER the key
    check, BEFORE the network call. A spent budget blocks the query and
    returns [] without hitting the network -- loudly logged so a
    silently-empty scan is never mistaken for "no good jobs today".
    `budget=None` (the default) preserves old unlimited/unbudgeted behavior,
    so existing callers are unaffected."""
    api_key = api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("warning: SERPAPI_API_KEY not set; skipping serpapi source")
        return []
    if budget is not None and not budget.take():
        print(f"serpapi budget exhausted (cap {budget.monthly_cap}/mo, "
              f"{budget.used()} used this month); skipping remaining queries "
              f"-- dropped {query!r}")
        return []
    params = [f"engine=google_jobs", f"q={quote_plus(query)}", f"hl={hl}", "ltype=1",
              f"api_key={api_key}"]
    if location:
        params.append(f"location={quote_plus(location)}")
    data = _http_json("https://serpapi.com/search?" + "&".join(params))
    out: list[JobPosting] = []
    for j in data.get("jobs_results", []):
        org = j.get("company_name") or ""
        out.append(JobPosting(
            source="serpapi", org=org,
            title=j.get("title") or "", location=j.get("location") or "",
            url=_best_company_url(org, j.get("apply_options"), j.get("share_link")),
            description=j.get("description") or "", raw_id=j.get("job_id") or "",
            apply_options=_apply_option_links(j.get("apply_options")),
        ))
    return out


# Free remote-job boards (no API key). Each fetcher takes one filter param so
# fetch_all can call it directly from a sources.yaml entry, mirroring the
# ashby/greenhouse/lever (slug, name) dispatch shape below. Every fetcher
# catches its own errors -- warns and returns [] -- so one dead board never
# kills the scan (ported from norina-jobs/src/norina/boards.py).
_BOARD_UA = "cv-tailor/0.3 (job scan; contact@teodorlutoiu.com)"


def fetch_remotive(category: str) -> list[JobPosting]:
    """Remotive public API, one category at a time (e.g. 'software-dev', 'marketing')."""
    try:
        data = _http_json(f"https://remotive.com/api/remote-jobs?category={quote_plus(category)}")
    except Exception as e:
        print(f"warning: remotive fetch failed for category={category!r}: {e}")
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs", []):
        out.append(JobPosting(
            source="remotive", org=j.get("company_name") or "",
            title=j.get("title") or "",
            location=f"Remote - {j.get('candidate_required_location') or 'Anywhere'}",
            url=j.get("url") or "", description=_strip_html(j.get("description") or ""),
            raw_id=str(j.get("id") or j.get("url") or ""),
        ))
    return out


def fetch_remoteok(tag: str) -> list[JobPosting]:
    """RemoteOK public API filtered by a single tag. Element 0 of the response is
    always a legal notice, not a job -- skip it."""
    try:
        req = urllib.request.Request(
            f"https://remoteok.com/api?tags={quote_plus(tag)}",
            headers={"Accept": "application/json", "User-Agent": _BOARD_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
    except Exception as e:
        print(f"warning: remoteok fetch failed for tag={tag!r}: {e}")
        return []
    out: list[JobPosting] = []
    for j in data[1:] if isinstance(data, list) else []:
        out.append(JobPosting(
            source="remoteok", org=j.get("company") or "",
            title=j.get("position") or "",
            location=f"Remote - {j.get('location') or 'Anywhere'}",
            url=j.get("url") or "", description=_strip_html(j.get("description") or ""),
            raw_id=str(j.get("id") or ""),
        ))
    return out


def fetch_jobicy(count: int, tag: str) -> list[JobPosting]:
    """Jobicy public API filtered by tag, capped at count results."""
    try:
        data = _http_json(
            f"https://jobicy.com/api/v2/remote-jobs?count={count}&tag={quote_plus(tag)}")
    except Exception as e:
        print(f"warning: jobicy fetch failed for tag={tag!r}: {e}")
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs", []):
        out.append(JobPosting(
            source="jobicy", org=j.get("companyName") or "",
            title=j.get("jobTitle") or "",
            location=f"Remote - {j.get('jobGeo') or 'Anywhere'}",
            url=j.get("url") or "", description=_strip_html(j.get("jobDescription") or ""),
            raw_id=str(j.get("id") or ""),
        ))
    return out


def _parse_wwr_rss(root: ET.Element) -> list[JobPosting]:
    out: list[JobPosting] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        org, _, role = title.partition(":")
        if not role:
            org, role = "", title
        region = (item.findtext("region") or "").strip()
        out.append(JobPosting(
            source="wwr", org=org.strip(), title=role.strip(),
            location=f"Remote - {region or 'Anywhere'}",
            url=(item.findtext("link") or "").strip(),
            description=_strip_html(item.findtext("description") or ""),
            raw_id=(item.findtext("guid") or item.findtext("link") or "").strip(),
        ))
    return out


def fetch_wwr(category: str) -> list[JobPosting]:
    """We Work Remotely RSS feed for one category slug (e.g. 'programming',
    'sales-and-marketing')."""
    url = f"https://weworkremotely.com/categories/remote-{category}-jobs.rss"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _BOARD_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            root = ET.fromstring(resp.read())
    except Exception as e:
        print(f"warning: wwr fetch failed for category={category!r}: {e}")
        return []
    return _parse_wwr_rss(root)


# --- arbeitnow + himalayas (keyless too, ported from the htgaj pipeline) ------
#
# Neither API takes a filter param the scan can use (measured 2026-08-17:
# arbeitnow silently IGNORES ?remote=true and himalayas silently IGNORES ?q=,
# both returning the unfiltered feed), so their only knob is how much to pull.
_ARBEITNOW_MAX_PAGES = 10          # bounds a sources.yaml typo (pages: 500)
_HIMALAYAS_PAGE_SIZE = 20          # the API's hard ceiling: limit=50 still returns 20
_HIMALAYAS_MAX_COUNT = 200         # bounds a sources.yaml typo (count: 100000)
_LOCATION_WIDTH = 200              # one posting cannot own the queue entry / digest


def _first_id(*candidates) -> str:
    """First non-empty candidate as a stripped string, else "" -- the same
    `id or url` fallback chain fetch_remotive/fetch_wwr use, hoisted because
    for these two boards an empty raw_id is likely rather than theoretical
    (himalayas ships no id and no slug field at all).

    An empty raw_id is not cosmetic: seen_jobs is `PRIMARY KEY (source,
    raw_id)` written with INSERT OR IGNORE, so the first blank one is stored
    and every later blank one from the same source then reads as already-seen
    in cache.is_new. Callers DROP a posting this returns "" for -- a posting
    with no stable id can never be deduped correctly anyway."""
    for c in candidates:
        s = str(c or "").strip()
        if s:
            return s
    return ""


def _flag(value) -> bool:
    """Truthiness for a JSON flag that decides real behavior. arbeitnow's
    `remote` is a real bool today; a future response spelling it "false" as a
    STRING would make bool() report every onsite job remote, which is the one
    direction that matters here (it would send applications to onsite roles)."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y")
    return bool(value)


def fetch_arbeitnow(pages: int = 1) -> list[JobPosting]:
    """Arbeitnow's free public board API, `pages` pages of ~100-175 postings.

    Two measured facts drive the mapping (probed 2026-08-17):

    * `remote` is a per-posting boolean and only ~6% are true. This is a
      European board, NOT a remote-only one, and `?remote=true` is ignored by
      the API, so there is no server-side filter to lean on. Remoteness is
      therefore REPORTED, never fabricated: only a remote posting gets the
      "Remote - " prefix gates.is_remote looks for, so the onsite majority is
      dropped by Gate 1 instead of being smuggled past it.
    * every posting is European (locations are EU/UK cities). That board-level
      fact is worth encoding into `location`, because Gate 1's EU check
      otherwise has nothing to match in a German-language posting that never
      says "Europe".

    A page that fails keeps the pages already fetched (partial beats nothing)
    and a total failure warns and returns [] -- one dead board never kills the
    scan."""
    pages = max(1, min(int(pages or 1), _ARBEITNOW_MAX_PAGES))
    out: list[JobPosting] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        try:
            data = _http_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        except Exception as e:
            print(f"warning: arbeitnow fetch failed for page={page}: {e}")
            break
        rows = (data.get("data") or []) if isinstance(data, dict) else []
        if not rows:
            break
        for j in rows:
            if not isinstance(j, dict):
                continue
            raw_id = _first_id(j.get("slug"), j.get("url"))
            if not raw_id or raw_id in seen:
                continue
            seen.add(raw_id)
            city = re.sub(r"\s+", " ", str(j.get("location") or "")).strip()[:_LOCATION_WIDTH]
            where = f"Europe ({city})" if city else "Europe"
            out.append(JobPosting(
                source="arbeitnow", org=str(j.get("company_name") or ""),
                title=str(j.get("title") or ""),
                location=f"Remote - {where}" if _flag(j.get("remote")) else where,
                url=str(j.get("url") or ""),
                description=_strip_html(j.get("description") or ""),
                raw_id=raw_id,
            ))
    return out


def fetch_himalayas(count: int = _HIMALAYAS_PAGE_SIZE) -> list[JobPosting]:
    """Himalayas' free public API, up to `count` postings paged by offset.

    Three measured facts (probed 2026-08-17), each one a trap the htgaj
    adapter this is ported from walks into:

    * there is NO `id` and NO `slug` field. That adapter keyed on
      `id or slug`, which today blanks the raw_id of every posting. The
      stable identifier is `guid` (the posting URL), with applicationLink as
      the fallback -- see _first_id for why a blank raw_id is worse than a
      dropped posting.
    * `limit` is capped at 20 server-side (limit=50 returns 20), so `count`
      is paged 20 at a time via the `offset` the response echoes back.
    * `q=` is IGNORED -- that adapter's five role queries all fetched the same
      first 20 postings, so this uses offsets instead of query terms.

    `locationRestrictions` is the geo signal Gate 1 reads (["Germany"] passes,
    ["United States"] does not, [] means anywhere)."""
    count = max(1, min(int(count or _HIMALAYAS_PAGE_SIZE), _HIMALAYAS_MAX_COUNT))
    out: list[JobPosting] = []
    seen: set[str] = set()
    for offset in range(0, count, _HIMALAYAS_PAGE_SIZE):
        try:
            data = _http_json("https://himalayas.app/jobs/api"
                              f"?limit={_HIMALAYAS_PAGE_SIZE}&offset={offset}")
        except Exception as e:
            print(f"warning: himalayas fetch failed for offset={offset}: {e}")
            break
        rows = (data.get("jobs") or []) if isinstance(data, dict) else []
        for j in rows:
            if not isinstance(j, dict):
                continue
            # guid and applicationLink are the same posting URL today; there is
            # no separate direct-apply link to promote into apply_options.
            #
            # The two _first_id chains below are DELIBERATELY reversed, and
            # only matter on the day the fields stop agreeing: raw_id wants the
            # STABLE identity (guid, the canonical posting URL) because it is a
            # seen_jobs primary key, while url wants the ACTIONABLE target
            # (applicationLink, where a human or the portal adapter is sent).
            # Each falls back to the other so neither can end up empty.
            raw_id = _first_id(j.get("guid"), j.get("applicationLink"))
            if not raw_id or raw_id in seen:
                continue
            seen.add(raw_id)
            restrictions = j.get("locationRestrictions")
            if isinstance(restrictions, str):
                restrictions = [restrictions]
            where = ", ".join(str(r).strip() for r in (restrictions or []) if str(r).strip())
            out.append(JobPosting(
                source="himalayas", org=str(j.get("companyName") or ""),
                title=str(j.get("title") or ""),
                location=f"Remote - {where[:_LOCATION_WIDTH] or 'Anywhere'}",
                url=_first_id(j.get("applicationLink"), j.get("guid")),
                description=_strip_html(j.get("description") or ""),
                raw_id=raw_id,
            ))
        # A short page means the board is exhausted -- asking for the next
        # offset would just re-request an empty tail.
        if len(rows) < _HIMALAYAS_PAGE_SIZE:
            break
    return out


# --- adzuna (credentialed, ten EU markets) -----------------------------------
#
# The ONE genuinely large EU supply source available to this scan: a single
# query returned four-figure counts on gb/it and three-figure on de/es/pl
# (probed 2026-08-17). Keys are read from the environment only -- never from
# any committed config file.
#
# Values are the ENGLISH country name, because that is what gates._EU_RE
# matches and the API's own location text is in the LOCAL language (measured:
# location.area[0] is "Deutschland" / "Nederland" / "Polska", none of which
# _EU_RE knows). Only these ten markets are accepted: Adzuna also serves
# us/au/br/in/etc, and the code goes into the request URL's PATH, so anything
# outside the allowlist is refused rather than interpolated.
ADZUNA_MARKETS = {
    "at": "Austria", "be": "Belgium", "ch": "Switzerland", "de": "Germany",
    "es": "Spain", "fr": "France", "gb": "United Kingdom", "it": "Italy",
    "nl": "Netherlands", "pl": "Poland",
}
_ADZUNA_PAGE_SIZE = 50    # the API's hard ceiling: results_per_page=100 returns 50
_ADZUNA_MAX_PAGES = 5     # bounds a sources.yaml typo (pages: 500)


def _redact(text, *secrets) -> str:
    """Blank every secret out of a string bound for stdout.

    urllib stringifies the URL it failed on, and an Adzuna URL carries BOTH
    app_id and app_key as query params -- so a bare `{e}` in a warning writes
    two live credentials into scans/logs/<date>.log, which is exactly the
    class of leak SEC020/SEC022 covers.

    Both the raw AND the quote_plus form of each secret are blanked, because
    the credential reaches the URL percent-encoded. Today's keys are hex, for
    which quote_plus is the identity function and the distinction is invisible
    -- which is exactly why it has to be handled now rather than discovered
    later by a key containing `+`, `/` or `=`."""
    out = str(text)
    for s in secrets:
        if not s:
            continue
        for form in {str(s), quote_plus(str(s))}:
            out = out.replace(form, "***")
    return out


def _strip_query(url: str) -> str:
    """Posting URL with query string and fragment removed -- for IDENTITY only.

    Used for the raw_id fallback, never for the navigation target. MEASURED
    2026-08-17: 30 of 53 sampled Adzuna postings carry an `se=` param whose
    value is a PER-REQUEST session token (three consecutive requests returned
    three different values for the same posting id), so an `se` inside a raw_id
    would make every scan re-see the same posting as new and defeat
    cache.is_new entirely. `v=` is stable per posting but adds nothing to an
    identity that already has the numeric id or the bare path.

    This is the same identity-vs-target split fetch_himalayas documents: the
    id must be stable, the URL must be usable."""
    return str(url or "").split("#", 1)[0].split("?", 1)[0]


def _drop_params_valued(url: str, *values) -> str:
    """`url` with every query param whose VALUE equals one of `values` removed;
    order, the remaining params and the fragment are preserved.

    This is the narrow fix for a real leak: MEASURED over 53 postings across
    gb/de/nl/pl, Adzuna's `redirect_url` embeds ADZUNA_APP_ID as `utm_source`
    -- and in `utm_source` ONLY, in every single sample. Since job.url is
    persisted into the scout queue JSON, scans/<date>.json and the Telegram
    digest, and rendered as an <a href> on /scout, that param has to go.

    Everything else STAYS. The same sample showed two query shapes --
    `utm_medium&utm_source` (23) and `se&utm_medium&utm_source&v` (30) -- and
    nobody has shown that `se` or `v` are droppable. job.url becomes the
    apply_target, so discarding a load-bearing param would break every Adzuna
    application with nothing in the suite or the log to say so. Matching on the
    VALUE rather than on the name `utm_source` also means the guard survives
    Adzuna renaming the param."""
    drop = {str(v) for v in values if v}
    head, sep, tail = str(url or "").partition("?")
    if not sep or not drop:
        return head + sep + tail
    query, hsep, frag = tail.partition("#")
    kept = []
    for pair in query.split("&"):
        raw_value = pair.partition("=")[2]
        if raw_value in drop or unquote_plus(raw_value) in drop:
            continue
        kept.append(pair)
    return head + (("?" + "&".join(kept)) if kept else "") + hsep + frag


def fetch_adzuna(query: str, country: str = "gb",
                 results_per_page: int = _ADZUNA_PAGE_SIZE, pages: int = 1,
                 max_days_old: int | None = None,
                 app_id: str | None = None, app_key: str | None = None) -> list[JobPosting]:
    """Adzuna's search API for one EU market, `pages` pages of up to 50 postings.

    Credentials come from ADZUNA_APP_ID / ADZUNA_APP_KEY and are checked FIRST,
    before the market allowlist and before any network call, so an unset key
    returns [] loudly instead of failing halfway (the fetch_serpapi contract --
    see its docstring for why that ordering matters here).

    Four measured facts drive the request shape (probed 2026-08-17):

    * `where` expects a PLACE, not a work mode. `where=remote` returns
      `count: 0` -- indistinguishable from a dead credential. Remote filtering
      therefore lives in the `what` full-text terms and `where` is never sent.
    * `results_per_page` is capped at 50 server-side (100 returns 50), and
      pages are walked via the number in the URL path (`/search/2`), which
      returned 150 unique postings over 3 pages with zero overlap.
    * unknown query params are rejected with HTTP 400 and bad credentials with
      HTTP 401 -- this API fails loudly, unlike arbeitnow/himalayas which
      silently ignore filters they do not support. A 200 that yields zero rows
      on page 1 is still warned about: the request was metered, and
      `where=remote` looks exactly like an empty market.
    * `redirect_url` comes in TWO query shapes -- `utm_medium&utm_source` and
      `se&utm_medium&utm_source&v` (23 and 30 of 53 sampled postings) -- and
      ADZUNA_APP_ID rides in `utm_source`, in `utm_source` only, in every
      sample. Only that one param is dropped from job.url; `se` and `v` are
      kept because nothing shows they are droppable and job.url becomes the
      apply_target. `se` DOES rotate per request, so the raw_id fallback drops
      the whole query instead (see _strip_query / _drop_params_valued).
    * `sort_by=date` + `max_days_old` both work (max_days_old=7 cut a query
      from 1304 to 219 hits). Unsorted results run back nine months, so a
      daily scan asks for the newest first.

    And two the mapping has to absorb:

    * there is NO remote flag anywhere in the payload (field census over 200
      live postings). Remoteness is REPORTED from the posting's own
      title+description using the very predicate Gate 1 will apply, so the
      "Remote - " prefix only promotes evidence Gate 1 cannot reach on its own
      (it never reads the title) and never invents any.
    * `description` is hard-capped at 500 chars server-side and ellipsis-
      truncated, so the LLM scorer and cover-letter writer see an excerpt.
      That is a recall/quality limit to know about, not something to fix here.
    """
    app_id = app_id or os.environ.get("ADZUNA_APP_ID")
    app_key = app_key or os.environ.get("ADZUNA_APP_KEY")
    missing = [name for name, val in (("ADZUNA_APP_ID", app_id), ("ADZUNA_APP_KEY", app_key))
               if not val]
    if missing:
        print(f"warning: {' and '.join(missing)} not set; skipping adzuna source")
        return []
    market = ADZUNA_MARKETS.get(country) if isinstance(country, str) else None
    if not market:
        print(f"warning: unsupported adzuna country {country!r}; skipping "
              f"(supported: {', '.join(sorted(ADZUNA_MARKETS))})")
        return []

    per_page = max(1, min(int(results_per_page or _ADZUNA_PAGE_SIZE), _ADZUNA_PAGE_SIZE))
    pages = max(1, min(int(pages or 1), _ADZUNA_MAX_PAGES))
    params = [f"app_id={quote_plus(app_id)}", f"app_key={quote_plus(app_key)}",
              f"what={quote_plus(query or '')}", f"results_per_page={per_page}",
              "sort_by=date"]
    try:
        days = int(max_days_old) if max_days_old else 0
    except (TypeError, ValueError):
        days = 0
    if days > 0:
        params.append(f"max_days_old={days}")

    out: list[JobPosting] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        url = (f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?"
               + "&".join(params))
        try:
            data = _http_json(url)
        except Exception as e:
            print(f"warning: adzuna fetch failed for country={country!r} page={page}: "
                  f"{_redact(e, app_id, app_key)}")
            break
        if not isinstance(data, dict) or "results" not in data:
            print(f"warning: unexpected adzuna payload for country={country!r} "
                  f"page={page} (no 'results' key); a metered request was already "
                  f"spent -- treating as no results")
            break
        rows = data.get("results") or []
        if not rows:
            # An empty page 2+ is ordinary exhaustion. An empty page 1 means a
            # request was issued and returned nothing, which must never read as
            # "this market has no jobs" -- `where=remote` and a wrong market
            # both look exactly like this. MEMORY.md: an empty output is not
            # evidence of an empty input.
            if page == 1:
                print(f"warning: adzuna returned 0 postings for country={country!r} "
                      f"query={query!r} (count={data.get('count')!r}); an empty "
                      f"result is not evidence of an empty market -- check the query "
                      f"terms, and that no work mode leaked into `where`")
            break
        for j in rows:
            if not isinstance(j, dict):
                continue
            # redirect_url is the ONLY link Adzuna offers -- it is both the
            # posting url and the apply target, so there is no second, distinct
            # apply link to promote into apply_options.
            #
            # Two DIFFERENT derivations of it, deliberately (see the two
            # helpers): the navigation target keeps every param except the one
            # carrying the credential, while the identity fallback keeps none,
            # because `se=` rotates per request.
            redirect = str(j.get("redirect_url") or "")
            link = _drop_params_valued(redirect, app_id)
            raw_id = _first_id(j.get("id"), _strip_query(redirect))
            if not raw_id or raw_id in seen:
                continue
            seen.add(raw_id)
            company = j.get("company") if isinstance(j.get("company"), dict) else {}
            loc = j.get("location") if isinstance(j.get("location"), dict) else {}
            city = re.sub(r"\s+", " ", str(loc.get("display_name") or "")).strip()
            where = f"{market} ({city[:_LOCATION_WIDTH]})" if city else market
            title = str(j.get("title") or "")
            description = _strip_html(j.get("description") or "")
            out.append(JobPosting(
                source="adzuna", org=str(company.get("display_name") or ""),
                title=title,
                location=f"Remote - {where}" if is_remote(title, description) else where,
                url=link, description=description, raw_id=raw_id,
            ))
    return out


# --- jsearch (credentialed, UK-targeted, budget-aware) -----------------------
#
# JSearch via OpenWebNinja. UK ONLY, and that is a measurement, not a
# preference (probed 2026-08-17): the API's default search context is the US
# (`parameters.country` echoes "us" for a UK query), "python developer remote
# germany" returns 0 rows, and "AI engineer remote europe" returns US
# employers. `country=gb` IS accepted and echoed back -- but on its own it
# returns 0 rows, so UK targeting has to be in the query TEXT and the country
# param only sharpens it. Continental queries are pure waste at 200 req/mo per
# key, so the market map is the gate: widening it is a deliberate edit.
JSEARCH_MARKETS = {"gb": "United Kingdom"}
_JSEARCH_URL = "https://api.openwebninja.com/jsearch/search"
# Left at 1 on purpose: whether num_pages>1 bills as one request or as N is not
# something this project can measure from outside, and an undercounting budget
# is worse than a smaller page.
_JSEARCH_NUM_PAGES = 1
# HTTP codes that mean "this key is no good right now" rather than "the query
# failed". MEASURED: an invalid or spent key answers 401 (a bogus key and a
# missing header both did), which urllib RAISES -- so the htgaj adapter's
# `data["status"] == "FAIL"` / error-code branch could never fire and the query
# was simply lost.
_JSEARCH_DEAD_KEY_CODES = (401, 403, 429)

# Round-robin state, per process. Six keys exist so the load can be spread;
# spending one down while five sit idle is exactly how the htgaj run burned
# four of them. `dead` is deliberately process-scoped and NOT persisted: the
# four keys that file labels `status: exhausted` (dated 2026-01-31) all work
# today, so a durable exhausted-list is how a working key stays retired for
# seven months.
_JSEARCH_ROTATION: dict = {"cursor": 0, "dead": set()}


def reset_jsearch_key_rotation() -> None:
    """Forget the round-robin cursor and the dead-key set (tests; also the
    honest way to retry a key after a monthly quota reset)."""
    _JSEARCH_ROTATION["cursor"] = 0
    _JSEARCH_ROTATION["dead"] = set()


def _jsearch_keys(api_keys=None) -> list[str]:
    """Keys from JSEARCH_API_KEYS (comma-separated) or an explicit list.

    Env ONLY -- there is deliberately no fallback to any config file. The
    sibling project's key file is committed to git, and reading a credential
    out of a tracked file is a live SEC020/SEC022 finding, so the file is not
    referenced from this module at all."""
    raw = api_keys if api_keys is not None else os.environ.get("JSEARCH_API_KEYS", "")
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(k).strip() for k in (raw or []) if str(k).strip()]


def _jsearch_next_key(keys: list[str]) -> str | None:
    """Next live key in round-robin order, advancing the cursor. None when
    every key has been marked dead this process."""
    dead = _JSEARCH_ROTATION["dead"]
    for _ in range(len(keys)):
        key = keys[_JSEARCH_ROTATION["cursor"] % len(keys)]
        _JSEARCH_ROTATION["cursor"] = (_JSEARCH_ROTATION["cursor"] + 1) % len(keys)
        if key not in dead:
            return key
    return None


def _jsearch_apply_options(raw) -> list[dict]:
    """jsearch's apply_options are {apply_link, is_direct, publisher}; this
    repo's normalizer reads {link, title} (SerpAPI's shape). Without the remap
    _apply_option_links drops every option silently and /scout shows a human no
    apply link at all."""
    return _apply_option_links([
        {"link": (o or {}).get("apply_link"), "title": (o or {}).get("publisher")}
        for o in (raw or []) if isinstance(o, dict)])


def fetch_jsearch(query: str, country: str = "gb", api_keys=None,
                  budget=None) -> list[JobPosting]:
    """JSearch (OpenWebNinja) for one UK-targeted query, one page (~10 rows).

    Keys are checked FIRST -- before the market map, before the budget and
    before any network call -- so an unset JSEARCH_API_KEYS returns [] without
    consuming a budget slot (the fetch_serpapi contract; see its docstring).

    `budget`, when given, is a budget.JSearchBudget consulted BEFORE each HTTP
    attempt, retries included: a rejected key still cost a real request against
    the shared 1,200/mo pool, so the counter must not undercount it.

    Rejected keys (401/403/429) are retried on the NEXT key, up to one attempt
    per key, and marked dead for the rest of the process. Two facts shape that:
    the rejection arrives as an HTTP status urllib raises (not as a `status:
    FAIL` body, which is what the sibling adapter watched for and why a lost
    query looked like an empty one); and the dead-set is never persisted,
    because the four keys the sibling project recorded as `exhausted` on
    2026-01-31 all answer HTTP 200 today.

    Mapping notes, all measured over a full live page:

    * `job_city`, `job_state` AND `job_country` are null on every row. The
      sibling adapter builds its location from exactly those three and would
      emit "" for every posting, leaving Gate 1 no geo signal. `job_location`
      is the real field, and the market's English name is appended so
      gates._EU_RE has something to match ("Manchester" alone does not).
    * `job_google_link` is a google.com SERP URL. It is never used as the
      posting url -- a portal adapter cannot act on it -- so a row with no real
      apply link is dropped instead.
    """
    keys = _jsearch_keys(api_keys)
    if not keys:
        print("warning: JSEARCH_API_KEYS not set; skipping jsearch source")
        return []
    market = JSEARCH_MARKETS.get(country) if isinstance(country, str) else None
    if not market:
        print(f"warning: unsupported jsearch country {country!r}; skipping "
              f"(supported: {', '.join(sorted(JSEARCH_MARKETS))})")
        return []

    url = (f"{_JSEARCH_URL}?query={quote_plus(query or '')}"
           f"&num_pages={_JSEARCH_NUM_PAGES}&page=1&country={quote_plus(country)}")
    data = None
    for _ in range(len(keys)):
        key = _jsearch_next_key(keys)
        if key is None:                # every key died, this call or an earlier one
            break
        if budget is not None and not budget.take():
            print(f"jsearch budget exhausted (cap {budget.monthly_cap}/mo, "
                  f"{budget.used()} used this month); skipping remaining queries "
                  f"-- dropped {query!r}")
            return []
        req = urllib.request.Request(url, headers={
            "x-api-key": key, "Accept": "application/json", "User-Agent": _BOARD_UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if e.code in _JSEARCH_DEAD_KEY_CODES:
                _JSEARCH_ROTATION["dead"].add(key)
                print(f"warning: a jsearch key was rejected (HTTP {e.code}); "
                      f"retiring it for this run and trying the next")
                continue
            print(f"warning: jsearch fetch failed for {query!r}: "
                  f"HTTP {e.code} {_redact(e.reason, *keys)}")
            return []
        except Exception as e:
            print(f"warning: jsearch fetch failed for {query!r}: {_redact(e, *keys)}")
            return []
    if data is None:
        print(f"warning: no usable jsearch key left (all {len(keys)} rejected); "
              f"dropped {query!r}")
        return []

    # A budget unit has now been spent, so "nothing came back" must never be
    # allowed to read as "there are no UK jobs today". HTTP 401 covers a
    # rejected key (handled above), but every OTHER 200-with-an-error-body --
    # a malformed query, a plan change, an upstream outage -- would otherwise
    # be indistinguishable from an empty market. MEMORY.md carries this as an
    # explicit twice-bitten rule: an empty output is not evidence of an empty
    # input.
    payload = data if isinstance(data, dict) else {}
    if not isinstance(data, dict):
        print(f"warning: jsearch answered with a {type(data).__name__}, not an object, "
              f"for {query!r} (a metered request was already spent)")
    status = str(payload.get("status") or "").strip()
    if status and status.lower() not in ("ok", "success"):
        print(f"warning: jsearch answered HTTP 200 with status={status!r} for "
              f"{query!r} (a metered request was already spent); "
              f"treating as no results")
    rows = payload.get("data") or []
    if not rows:
        print(f"warning: jsearch returned 0 rows for {query!r} -- a metered request "
              f"produced nothing; an empty result is not evidence of an empty "
              f"market (check that the query names the UK)")

    out: list[JobPosting] = []
    seen: set[str] = set()
    for j in rows:
        if not isinstance(j, dict):
            continue
        options = _jsearch_apply_options(j.get("apply_options"))
        link = _first_id(j.get("job_apply_link"),
                         options[0]["url"] if options else "")
        raw_id = _first_id(j.get("job_id"), j.get("job_uid"), link)
        if not raw_id or raw_id in seen or not link:
            continue
        seen.add(raw_id)
        where = re.sub(r"\s+", " ", str(j.get("job_location") or "")).strip()
        where = (f"{where[:_LOCATION_WIDTH]}, {market}" if where else market)
        out.append(JobPosting(
            source="jsearch", org=str(j.get("employer_name") or ""),
            title=str(j.get("job_title") or ""),
            location=f"Remote - {where}" if _flag(j.get("job_is_remote")) else where,
            url=link, description=_strip_html(j.get("job_description") or ""),
            raw_id=raw_id, apply_options=options,
        ))
    return out


def fetch_all(sources: list[dict], serp_budget=None, jsearch_budget=None) -> list[JobPosting]:
    """sources = [{'kind': 'ashby'|'greenhouse'|'lever', 'slug': '...', 'name': '...'}, ...]

    `serp_budget`, when given (a budget.SerpBudget), is the ONE shared
    instance threaded through every serpapi source below, so the counter
    reflects queries actually taken across the whole scan rather than each
    source re-reading a stale file. `serp_budget=None` (the default) means
    unbudgeted -- every serpapi source fires -- matching pre-budget behavior
    for callers (including existing tests) that do not pass one.

    `jsearch_budget` (a budget.JSearchBudget) is the same arrangement for the
    jsearch sources: one shared instance for the whole scan, because a
    per-source instance re-reads a stale count and the 1,200/mo pool cap never
    actually binds."""
    dispatch = {
        "ashby": fetch_ashby_org,
        "greenhouse": fetch_greenhouse_org,
        "lever": fetch_lever_org,
    }
    board_dispatch = {
        "remotive": lambda s: fetch_remotive(s["category"]),
        "remoteok": lambda s: fetch_remoteok(s["tag"]),
        "jobicy": lambda s: fetch_jobicy(s.get("count", 50), s["tag"]),
        "wwr": lambda s: fetch_wwr(s["category"]),
        "arbeitnow": lambda s: fetch_arbeitnow(s.get("pages", 1)),
        "himalayas": lambda s: fetch_himalayas(s.get("count", _HIMALAYAS_PAGE_SIZE)),
        # No credentials read from `s` for either of the two below: a source
        # entry lives in the COMMITTED sources.yaml, so accepting app_id /
        # app_key / api_keys there at all would invite someone to put live
        # values in a tracked file (SEC020). Env only.
        "adzuna": lambda s: fetch_adzuna(
            s["query"], country=s.get("country", "gb"),
            results_per_page=s.get("results_per_page", _ADZUNA_PAGE_SIZE),
            pages=s.get("pages", 1), max_days_old=s.get("max_days_old")),
        "jsearch": lambda s: fetch_jsearch(
            s["query"], country=s.get("country", "gb"), budget=jsearch_budget),
    }
    out: list[JobPosting] = []
    for s in sources:
        kind = s["kind"]
        if kind == "serpapi":
            try:
                out.extend(fetch_serpapi(s["query"], s.get("location"), budget=serp_budget))
            except Exception as e:
                print(f"warning: serpapi fetch failed for {s.get('query')!r}: {e}")
            continue
        if kind in board_dispatch:
            try:
                out.extend(board_dispatch[kind](s))
            except Exception as e:
                print(f"warning: {kind} fetch failed (bad source config?): {e}")
            continue
        fn = dispatch.get(kind)
        if not fn:
            print(f"warning: unknown source kind {kind!r}; skipping")
            continue
        try:
            out.extend(fn(s["slug"], s.get("name")))
        except Exception as e:
            print(f"warning: fetch failed for {s.get('name', s['slug'])}: {e}")
    return out
