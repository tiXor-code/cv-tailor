# tests/test_job_sources_new_boards.py
"""HN "Who is hiring", YC public remote jobs and the startup.jobs MCP free tier
(added 2026-09-24). All fixture data is fictional -- this repo is public."""
import html
import io
import json
import pathlib
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources  # noqa: E402


def _fake(payload):
    raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    buf = io.BytesIO(raw.encode() if isinstance(raw, str) else raw)
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    return cm


def _router(routes, calls=None):
    """urlopen side_effect: payload picked by URL substring (and, for POSTs,
    by a substring of the request body). An unrouted request fails the test."""
    def _open(req, *a, **kw):
        url = getattr(req, "full_url", None) or str(req)
        body = (getattr(req, "data", None) or b"").decode()
        if calls is not None:
            calls.append((url, body))
        for key, payload in routes.items():
            frag, _, body_frag = key.partition("#")
            if frag in url and (not body_frag or body_frag in body):
                if callable(payload):
                    payload = payload()
                if isinstance(payload, Exception):
                    raise payload
                return _fake(payload)
        raise AssertionError(f"unexpected request {url} {body[:80]}")
    return _open


# --- Hacker News "Who is hiring" ------------------------------------------

def _hn_story(days_old=3, title="Ask HN: Who is hiring? (September 2026)", oid="900"):
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"hits": [{"objectID": "899", "title": "Ask HN: Who wants to be hired? (September 2026)",
                      "created_at": created},
                     {"objectID": oid, "title": title, "created_at": created}]}


_HN_TREE = {"id": 900, "children": [
    {"id": 1001, "text": "Fixture Co | Founding AI Engineer | REMOTE (EU timezones) | Full-time<p>"
                         "We ship agentic workflows with Claude Code. Apply: "
                         "<a href=\"https://jobs.ashbyhq.com/fixtureco/abc\">jobs.ashbyhq.com/fixtureco/abc</a>"
                         " or read <a href=\"https://fixture.example.com/about\">about us</a>."},
    {"id": 1002, "text": "Examplify | Berlin, Germany | Senior Product Engineer | ONSITE<p>"
                         "Build fixture features for fixture customers every single day. "
                         "Email ada@example.com"},
    {"id": 1003, "text": "too short"},
    {"id": 1004, "text": None, "deleted": True},
]}


def test_hn_maps_top_level_posts():
    routes = {"search_by_date": _hn_story(), "items/900": _HN_TREE}
    with mock.patch("urllib.request.urlopen", side_effect=_router(routes)):
        jobs = job_sources.fetch_hn_whoishiring()
    assert [j.raw_id for j in jobs] == ["1001", "1002"]
    j = jobs[0]
    assert j.source == "hn" and j.org == "Fixture Co"
    assert j.title == "Founding AI Engineer"
    assert "REMOTE (EU timezones)" in j.location
    assert "Claude Code" in j.description and "<a" not in j.description
    # the ATS link wins over the HN item page
    assert j.url == "https://jobs.ashbyhq.com/fixtureco/abc"
    assert {o["url"] for o in j.apply_options} == {"https://jobs.ashbyhq.com/fixtureco/abc",
                                                   "https://fixture.example.com/about"}
    k = jobs[1]
    assert k.title == "Senior Product Engineer"     # the role-looking segment, not "Berlin"
    assert k.url == "https://news.ycombinator.com/item?id=1002"


def test_hn_reads_links_with_escaped_slashes():
    """Live HN text escapes the slashes inside links: href="https:&#x2F;&#x2F;..."
    (measured 2026-09-25: 0 of 256 posts yielded a link before this)."""
    tree = {"id": 900, "children": [{"id": 2001, "text":
        "Fixture Co | AI Engineer | REMOTE<p>Build fixture agents all day long with fixture tools. "
        "<a href=\"https:&#x2F;&#x2F;jobs.lever.co&#x2F;fixtureco&#x2F;123\" rel=\"nofollow\">"
        "https:&#x2F;&#x2F;jobs.lever.co&#x2F;fixtureco&#x2F;123</a>"}]}
    with mock.patch("urllib.request.urlopen",
                    side_effect=_router({"search_by_date": _hn_story(), "items/900": tree})):
        jobs = job_sources.fetch_hn_whoishiring()
    assert jobs[0].url == "https://jobs.lever.co/fixtureco/123"


def test_hn_skips_a_stale_thread():
    calls = []
    routes = {"search_by_date": _hn_story(days_old=60), "items/900": _HN_TREE}
    with mock.patch("urllib.request.urlopen", side_effect=_router(routes, calls)):
        assert job_sources.fetch_hn_whoishiring(max_age_days=40) == []
    assert not any("items/" in u for u, _ in calls)


def test_hn_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
        assert job_sources.fetch_hn_whoishiring() == []


# --- YC public remote jobs -------------------------------------------------

def _yc_list_page(postings):
    data = {"props": {"jobPostings": postings}}
    return f'<div id="app" data-page="{html.escape(json.dumps(data), quote=True)}"></div>'


def _yc_job_page(desc):
    ld = {"@context": "https://schema.org/", "@type": "JobPosting", "title": "x", "description": desc}
    return f'<html><script type="application/ld+json" nonce="n1">{json.dumps(ld)}</script></html>'


_YC_POSTINGS = [
    {"id": 11, "title": "Founding Engineer", "url": "/companies/fixture-ai/jobs/aa-founding-engineer",
     "location": "Remote (US)", "companyName": "Fixture AI", "companyOneLiner": "Agents for fixtures"},
    {"id": 12, "title": "AI Product Engineer", "url": "/companies/examplify/jobs/bb-ai-product-engineer",
     "location": "Berlin, BE, DE / Remote", "companyName": "Examplify", "companyOneLiner": "Example travel"},
]


def test_yc_maps_postings_and_keeps_location_verbatim():
    routes = {
        "/jobs/role/software-engineer/remote": _yc_list_page(_YC_POSTINGS),
        "/jobs/role/science/remote": _yc_list_page([_YC_POSTINGS[0]]),   # dup across roles
        "aa-founding-engineer": _yc_job_page("<p>Ship with Claude Code.</p>"),
        "bb-ai-product-engineer": _yc_job_page("<p>Not a traditional coding role.</p>"),
    }
    with mock.patch("urllib.request.urlopen", side_effect=_router(routes)), \
            mock.patch("time.sleep"):
        jobs = job_sources.fetch_yc_jobs()
    assert [j.raw_id for j in jobs] == ["11", "12"]
    j = jobs[0]
    assert j.source == "yc" and j.org == "Fixture AI" and j.title == "Founding Engineer"
    assert j.location == "Remote (US)"                 # must reach the gate verbatim
    assert j.url == "https://www.ycombinator.com/companies/fixture-ai/jobs/aa-founding-engineer"
    assert "Ship with Claude Code." in j.description and "Agents for fixtures" in j.description


def test_yc_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")), mock.patch("time.sleep"):
        assert job_sources.fetch_yc_jobs() == []


# --- startup.jobs MCP --------------------------------------------------------

def _mcp_result(obj, sse=False):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(obj)}]}})
    return f"event: message\ndata: {body}\n\n" if sse else body


_SJ_SEARCH = {"jobs": [
    {"id": 1, "title": "AI Engineer", "company": {"name": "Fixture US"},
     "location": {"city": None, "state": "Kansas", "country": "U.S.", "country_code": "US"}},
    {"id": 2, "title": "Forward Deployed Engineer", "company": {"name": "Fixture PL"},
     "location": {"city": "Warsaw", "state": None, "country": "Poland", "country_code": "PL"}},
    {"id": 3, "title": "Product Engineer", "company": {"name": "Fixture Anywhere"}, "location": None},
], "next_cursor": None}


def _sj_job(i, name):
    return {"job": {"id": i, "title": f"Job {i}", "company": {"name": name},
                    "description": f"<p>Fixture description {i} with Claude Code.</p>",
                    "url": f"https://startup.jobs/fixture-{i}"}}


def test_startupjobs_keeps_eu_and_no_country_listings_only():
    routes = {
        "api.startup.jobs#initialize": json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}),
        "api.startup.jobs#search_jobs": _mcp_result(_SJ_SEARCH, sse=True),
        'api.startup.jobs#"id": 2': _mcp_result(_sj_job(2, "Fixture PL")),
        'api.startup.jobs#"id": 3': _mcp_result(_sj_job(3, "Fixture Anywhere")),
    }
    with mock.patch("urllib.request.urlopen", side_effect=_router(routes)), mock.patch("time.sleep"):
        jobs = job_sources.fetch_startupjobs(roles=("ai-engineer",), pages=1)
    assert sorted(j.raw_id for j in jobs) == ["2", "3"]           # the US job never reaches get_job
    pl = next(j for j in jobs if j.raw_id == "2")
    assert pl.source == "startupjobs" and pl.org == "Fixture PL"
    assert pl.location == "Remote - Poland"
    assert "Claude Code" in pl.description and "<p>" not in pl.description
    assert pl.url == "https://startup.jobs/fixture-2"
    assert next(j for j in jobs if j.raw_id == "3").location == "Remote - Anywhere"


def test_startupjobs_gives_up_cleanly_after_repeated_429():
    def _429():
        return urllib.error.HTTPError("https://api.startup.jobs/mcp", 429, "Too Many Requests", {}, None)
    sleeps = []
    with mock.patch("urllib.request.urlopen", side_effect=_router({"api.startup.jobs": _429})), \
            mock.patch("time.sleep", side_effect=lambda s: sleeps.append(s)):
        assert job_sources.fetch_startupjobs(roles=("ai-engineer",), pages=1) == []
    assert 15 in sleeps and 30 in sleeps


# --- dispatch ------------------------------------------------------------------

def test_fetch_all_dispatches_the_three_new_kinds():
    with mock.patch.object(job_sources, "fetch_hn_whoishiring", return_value=[]) as hn, \
            mock.patch.object(job_sources, "fetch_yc_jobs", return_value=[]) as yc, \
            mock.patch.object(job_sources, "fetch_startupjobs", return_value=[]) as sj:
        job_sources.fetch_all([
            {"kind": "hn_whoishiring", "max_age_days": 35},
            {"kind": "yc_jobs", "roles": ["software-engineer"]},
            {"kind": "startupjobs", "pages": 1, "max_details": 5},
        ])
    hn.assert_called_once_with(max_age_days=35)
    yc.assert_called_once_with(roles=("software-engineer",))
    assert sj.call_args.kwargs["pages"] == 1 and sj.call_args.kwargs["max_details"] == 5
