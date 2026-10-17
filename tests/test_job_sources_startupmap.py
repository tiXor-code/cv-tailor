# tests/test_job_sources_startupmap.py
"""StartupMap (startupmap.one), added 2026-10-17: one keyless listing endpoint
with no descriptions, so each kept listing's text is read from its own Ashby /
Greenhouse / Lever / Workable posting. Every request goes through a router
keyed on URL substrings and an unrouted request fails the test, so nothing
here reaches the network. All fixture data is fictional -- this repo is
public."""
import html
import io
import json
import pathlib
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources  # noqa: E402
from cv_tailor.job_sources import JobPosting  # noqa: E402

SM = "https://startupmap.one/api/jobs"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/"


def _uuid(i: int) -> str:
    return f"{i:08x}-0000-4000-8000-{i:012x}"


def _ago(**delta) -> str:
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _listing(jobs_id, apply_url, **over):
    """One StartupMap row, in the API's own shape (every key it sends)."""
    row = {"jobs_id": jobs_id, "startup_id": "fixtureco_de", "job_title": "AI Automation Engineer",
           "job_country": "Germany", "job_countries": ["Germany"], "location_match": "berlin",
           "location_matches": ["berlin"], "location_display": "Berlin", "is_remote": True,
           "workplace_type": "remote", "type": ["Engineering"], "apply_url": apply_url,
           "contact_email": None, "first_seen_at": _ago(hours=2), "created_at": _ago(hours=2),
           "posted_at": None, "promoted_until": None, "visible": True, "scrape_method": "ats:ashby"}
    row.update(over)
    return row


def _ashby_url(i: int, org: str = "fixtureco") -> str:
    return f"https://jobs.ashbyhq.com/{org}/{_uuid(i)}"


def _ashby_job(i: int, *, org="fixtureco", title="AI Automation Engineer", location="Europe",
               desc="<p>FixtureCo builds agentic workflows with Claude Code.</p>", pay=None):
    return {"id": _uuid(i), "title": title, "location": location, "secondaryLocations": [],
            "isRemote": True, "workplaceType": "Remote",
            "jobUrl": f"https://jobs.ashbyhq.com/{org}/{_uuid(i)}", "descriptionHtml": desc,
            "compensation": {"compensationTierSummary": pay,
                             "scrapeableCompensationSalarySummary": pay}}


def _fake(payload):
    raw = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    buf = io.BytesIO(raw.encode() if isinstance(raw, str) else raw)
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    return cm


def _router(routes, calls=None):
    """urlopen side_effect: the first route whose key is a substring of the URL
    answers. An unrouted request fails the test -- it would have gone out."""
    def _open(req, *a, **kw):
        url = getattr(req, "full_url", None) or str(req)
        if calls is not None:
            calls.append(url)
        for frag, payload in routes.items():
            if frag in url:
                if callable(payload):
                    payload = payload()
                if isinstance(payload, Exception):
                    raise payload
                return _fake(payload)
        raise AssertionError(f"unexpected request {url}")
    return _open


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The courtesy gap between ATS calls is real time; tests record it instead."""
    sleeps = []
    monkeypatch.setattr(job_sources.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def _run(routes, calls=None, **kw):
    with mock.patch("urllib.request.urlopen", side_effect=_router(routes, calls)):
        return job_sources.fetch_startupmap(**kw)


# --- the listing filters ------------------------------------------------------

def test_keeps_only_visible_remote_recent_in_scope_listings():
    rows = [
        _listing("keep-remote", _ashby_url(1)),
        _listing("keep-flag-only", _ashby_url(2), workplace_type=None),
        _listing("keep-engineering-and-sales", _ashby_url(3), type=["Engineering", "Sales"]),
        _listing("keep-no-category", _ashby_url(4), type=[]),
        _listing("drop-hidden", _ashby_url(5), visible=False),
        _listing("drop-hidden-as-string", _ashby_url(6), visible="false"),
        _listing("drop-onsite", _ashby_url(7), is_remote=False, workplace_type="onsite"),
        # is_remote says yes, workplace_type says hybrid: the explicit field wins
        _listing("drop-hybrid", _ashby_url(8), is_remote=True, workplace_type="hybrid"),
        _listing("drop-old", _ashby_url(9), first_seen_at=_ago(days=4)),
        _listing("drop-never-seen", _ashby_url(10), first_seen_at=None),
        _listing("drop-sales", _ashby_url(11), type=["Sales"]),
        _listing("drop-off-track", _ashby_url(12), type=["Legal", "Finance", "Recruiting & HR"]),
        _listing("drop-people", _ashby_url(13), type=["People"]),
        # Internship drops a listing even next to an in-scope category
        _listing("drop-intern", _ashby_url(14), type=["Internship", "Engineering"]),
        "not a row", None,
    ]
    board = {"jobs": [_ashby_job(i) for i in range(1, 15)]}
    calls = []
    jobs = _run({SM: rows, ASHBY_API + "fixtureco": board}, calls, gap_seconds=0)
    assert sorted(j.raw_id for j in jobs) == [
        "keep-engineering-and-sales", "keep-flag-only", "keep-no-category", "keep-remote"]
    # one listing GET, and one board call for the one org
    assert calls[0] == SM and len(calls) == 2


def test_age_window_follows_max_age_days():
    rows = [_listing("fresh", _ashby_url(1), first_seen_at=_ago(hours=20)),
            _listing("two-days", _ashby_url(2), first_seen_at=_ago(hours=40))]
    board = {"jobs": [_ashby_job(1), _ashby_job(2)]}
    jobs = _run({SM: rows, ASHBY_API: board}, max_age_days=1, gap_seconds=0)
    assert [j.raw_id for j in jobs] == ["fresh"]


def test_unsupported_and_unsafe_apply_urls_are_never_requested():
    rows = [
        _listing("personio", "https://fixtureco.jobs.personio.com/job/123"),
        _listing("plain-http", f"http://jobs.ashbyhq.com/fixtureco/{_uuid(1)}"),
        _listing("lookalike", f"https://jobs.ashbyhq.com.evil.example/fixtureco/{_uuid(1)}"),
        _listing("userinfo", f"https://evil.example@jobs.ashbyhq.com/fixtureco/{_uuid(1)}"),
        _listing("port", f"https://jobs.ashbyhq.com:8443/fixtureco/{_uuid(1)}"),
        _listing("backslash", f"https://evil.example\\.jobs.ashbyhq.com/fixtureco/{_uuid(1)}"),
        _listing("encoded-slug", f"https://jobs.ashbyhq.com/fixture%2Fco/{_uuid(1)}"),
        _listing("not-a-uuid", "https://jobs.ashbyhq.com/fixtureco/not-a-uuid"),
        _listing("gh-not-numeric", "https://job-boards.greenhouse.io/fixturegh/jobs/12ab"),
        _listing("no-url", None),
        _listing("", _ashby_url(2)),                    # no id: cannot be deduped, dropped
        _listing("ok", _ashby_url(3)),
    ]
    calls = []
    jobs = _run({SM: rows, ASHBY_API + "fixtureco": {"jobs": [_ashby_job(3)]}}, calls,
                gap_seconds=0)
    assert [j.raw_id for j in jobs] == ["ok"]
    assert all(u.startswith(("https://startupmap.one/", "https://api.ashbyhq.com/"))
               for u in calls), calls


# --- description resolution, one test per ATS --------------------------------

def test_ashby_board_is_fetched_once_per_org_and_matched_by_uuid():
    rows = [_listing("a", _ashby_url(1), job_title="AI Automation Engineer"),
            _listing("b", _ashby_url(2)),
            _listing("c", _ashby_url(3, org="otherco"), startup_id="otherco_se")]
    routes = {
        ASHBY_API + "fixtureco": {"jobs": [
            _ashby_job(1, title="AI Automation Engineer (EMEA)", pay="€70K - €90K"),
            _ashby_job(2, location="Berlin (Remote)")]},
        ASHBY_API + "otherco": {"jobs": [_ashby_job(3, org="otherco", desc="<p>Agents.</p>")]},
        SM: rows,
    }
    calls = []
    jobs = {j.raw_id: j for j in _run(routes, calls, gap_seconds=0)}
    board_calls = [u for u in calls if u.startswith(ASHBY_API)]
    assert len(board_calls) == 2 and all("includeCompensation=true" in u for u in board_calls)

    a = jobs["a"]
    assert a.source == "startupmap"
    assert a.url == _ashby_url(1)
    assert a.apply_options == [{"label": "Apply", "url": _ashby_url(1)}]
    assert a.title == "AI Automation Engineer (EMEA)"            # the ATS title wins
    assert a.org == "FixtureCo"                                  # spelled as the posting spells it
    assert a.location == "Remote - Europe"                       # Gate 1 reads the prefix
    assert "Claude Code" in a.description and "<p>" not in a.description
    assert "Compensation: €70K - €90K" in a.description
    assert jobs["b"].location == "Berlin (Remote)"               # already says remote
    assert jobs["c"].org == "Otherco"


def test_greenhouse_eu_and_us_postings_are_read_from_the_boards_api():
    rows = [_listing("eu", "https://job-boards.eu.greenhouse.io/fixturegh/jobs/4001",
                     startup_id="fixturegh_nl"),
            _listing("us", "https://job-boards.greenhouse.io/fixturegh/jobs/4002",
                     startup_id="fixturegh_nl")]
    content = html.escape("<h2>About</h2><p>Ship agentic workflows &amp; n8n flows.</p>")
    routes = {
        SM: rows,
        "boards-api.greenhouse.io/v1/boards/fixturegh/jobs/4001?content=true": {
            "id": 4001, "title": "Forward Deployed Engineer", "company_name": "Fixture GH",
            "location": {"name": "Remote - EMEA"}, "content": content},
        "boards-api.greenhouse.io/v1/boards/fixturegh/jobs/4002?content=true": {
            "id": 4002, "title": "AI Solutions Engineer", "company_name": "Fixture GH",
            "location": {"name": "Amsterdam"}, "content": content},
    }
    calls = []
    jobs = {j.raw_id: j for j in _run(routes, calls, gap_seconds=0)}
    assert not any("boards-api.eu.greenhouse.io" in u for u in calls)   # does not resolve
    eu = jobs["eu"]
    assert eu.org == "Fixture GH" and eu.title == "Forward Deployed Engineer"
    assert eu.location == "Remote - EMEA"
    assert eu.description == "About Ship agentic workflows & n8n flows."
    assert jobs["us"].location == "Remote - Amsterdam"


def test_lever_eu_postings_use_the_eu_api_and_every_text_field_is_read():
    eu_id, us_id = _uuid(21), _uuid(22)
    rows = [_listing("eu", f"https://jobs.eu.lever.co/fixturelv/{eu_id}", startup_id="fixturelv_pt"),
            _listing("us", f"https://jobs.lever.co/fixturelv/{us_id}", startup_id="fixturelv_pt")]
    posting = {
        "id": eu_id, "text": "AI Solutions Engineer",
        "categories": {"location": "Lisbon", "allLocations": ["Lisbon", "Remote - Portugal"]},
        "descriptionPlain": "", "description": "",
        "lists": [{"text": "What you'll do", "content": "<li>Build n8n flows</li>"}],
        "additionalPlain": "FixtureLV is\nremote-first.",
        "salaryRange": {"min": 60000, "max": 80000, "currency": "EUR", "interval": "per-year-salary"},
        "hostedUrl": f"https://jobs.eu.lever.co/fixturelv/{eu_id}",
    }
    routes = {SM: rows,
              f"api.eu.lever.co/v0/postings/fixturelv/{eu_id}": posting,
              f"api.lever.co/v0/postings/fixturelv/{us_id}": dict(
                  posting, id=us_id, categories={"location": "Porto"},
                  descriptionPlain="Intro text.", lists=[], additionalPlain="", salaryRange=None)}
    jobs = {j.raw_id: j for j in _run(routes, gap_seconds=0)}
    eu = jobs["eu"]
    assert eu.title == "AI Solutions Engineer"
    assert eu.location == "Lisbon, Remote - Portugal"
    assert "What you'll do Build n8n flows" in eu.description
    assert "FixtureLV is remote-first." in eu.description
    assert "Salary: 60000 - 80000 EUR per-year-salary" in eu.description
    assert jobs["us"].description == "Intro text."
    assert jobs["us"].location == "Remote - Porto"


def test_workable_reads_description_requirements_and_benefits():
    rows = [_listing("wk", "https://apply.workable.com/fixturewk/j/AB12CD34EF/",
                     startup_id="fixturewk_ro")]
    routes = {SM: rows,
              "apply.workable.com/api/v2/accounts/fixturewk/jobs/AB12CD34EF": {
                  "shortcode": "AB12CD34EF", "title": "AI Automation Specialist", "remote": True,
                  "location": {"city": "Bucharest", "country": "Romania"},
                  "locations": [{"city": "Bucharest", "country": "Romania", "hidden": False},
                                {"city": "Secret", "country": "Nowhere", "hidden": True}],
                  "state": "published", "workplace": "remote",
                  "description": "<p>Intro about Fixturewk.</p>",
                  "requirements": "<ul><li>Claude Code</li></ul>",
                  "benefits": "<p>Home office budget</p>"}}
    [job] = _run(routes, gap_seconds=0)
    assert job.title == "AI Automation Specialist"
    assert job.location == "Remote - Bucharest, Romania"
    assert job.description == "Intro about Fixturewk. Claude Code Home office budget"
    assert job.org == "Fixturewk"
    assert job.url == "https://apply.workable.com/fixturewk/j/AB12CD34EF/"


def test_a_failed_description_lookup_skips_only_that_posting(capsys):
    def _http(code):
        return urllib.error.HTTPError("https://example.invalid", code, "x", {}, None)

    rows = [
        _listing("gh-404", "https://job-boards.greenhouse.io/fixturegh/jobs/1", first_seen_at=_ago(hours=1)),
        _listing("dead-1", _ashby_url(1, org="deadco"), first_seen_at=_ago(hours=2)),
        _listing("dead-2", _ashby_url(2, org="deadco"), first_seen_at=_ago(hours=3)),
        _listing("closed", _ashby_url(3), first_seen_at=_ago(hours=4)),
        _listing("empty", "https://job-boards.greenhouse.io/fixturegh/jobs/2", first_seen_at=_ago(hours=5)),
        _listing("timeout", f"https://jobs.lever.co/fixturelv/{_uuid(9)}", first_seen_at=_ago(hours=6)),
        _listing("ok", "https://apply.workable.com/fixturewk/j/ZZ99ZZ99ZZ/", first_seen_at=_ago(hours=7)),
    ]
    routes = {
        SM: rows,
        "/jobs/1?content=true": _http(404),
        "/jobs/2?content=true": {"title": "AI Engineer", "content": ""},
        ASHBY_API + "deadco": _http(500),
        ASHBY_API + "fixtureco": {"jobs": [_ashby_job(4)]},          # job 3 is gone
        "api.lever.co": urllib.error.URLError("timed out"),
        "apply.workable.com/api/v2/accounts/fixturewk/jobs/ZZ99ZZ99ZZ": {
            "title": "AI Engineer", "description": "<p>Builds agents.</p>"},
    }
    calls = []
    jobs = _run(routes, calls, gap_seconds=0)
    assert [j.raw_id for j in jobs] == ["ok"]
    assert sum(1 for u in calls if "deadco" in u) == 1        # a dead board costs one request
    err = capsys.readouterr().err
    assert "6 description lookup(s) failed" in err
    for reason in ("HTTP 404", "HTTP 500 x2", "not on the board", "empty description", "URLError"):
        assert reason in err


def test_every_lookup_failing_is_reported_as_an_outage(capsys):
    rows = [_listing("a", "https://job-boards.greenhouse.io/fixturegh/jobs/1")]
    routes = {SM: rows, "boards-api.greenhouse.io": OSError("down")}
    assert _run(routes, gap_seconds=0) == []
    assert "every description lookup failed" in capsys.readouterr().err


@pytest.mark.parametrize("answer", [
    OSError("connection refused"),
    urllib.error.HTTPError(SM, 503, "Service Unavailable", {}, None),
    b"<html>maintenance</html>",
    {"jobs": []},
])
def test_listing_endpoint_failure_returns_empty_without_raising(answer, capsys):
    assert _run({SM: answer}) == []
    assert "warning: startupmap" in capsys.readouterr().err


def test_oversized_listing_response_is_refused(monkeypatch, capsys):
    monkeypatch.setattr(job_sources, "_STARTUPMAP_MAX_BYTES", 64)
    rows = [_listing("a", _ashby_url(1))]
    assert _run({SM: rows}) == []
    assert "larger than 64 bytes" in capsys.readouterr().err


def test_a_body_fetched_off_https_by_a_redirect_is_refused(capsys):
    class _Redirected(io.BytesIO):
        url = "http://startupmap.one/api/jobs"          # where urllib ended up

    cm = mock.MagicMock()
    cm.__enter__.return_value = _Redirected(json.dumps([_listing("a", _ashby_url(1))]).encode())
    with mock.patch("urllib.request.urlopen", return_value=cm):
        assert job_sources.fetch_startupmap() == []
    assert "redirected off https" in capsys.readouterr().err


def test_schema_drift_is_loud_not_silent(capsys):
    """A listing feed where nothing reads as visible+remote is far likelier to
    be a renamed field than a day with no remote jobs at 1,600 startups."""
    rows = [{"jobs_id": "x", "job_title": "AI Engineer", "remote": True}]
    assert _run({SM: rows}) == []
    assert "fields may have changed" in capsys.readouterr().err


def test_bad_config_never_raises_and_never_reaches_the_network(capsys):
    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not be called")):
        assert job_sources.fetch_startupmap(max_details="lots") == []
    assert "bad source config" in capsys.readouterr().err


# --- the lookup cap -------------------------------------------------------------

def test_max_details_caps_lookups_newest_first(_no_sleep):
    ages = {"1": 1, "2": 5, "3": 3, "4": 10}                     # hours since first seen
    rows = [_listing(f"gh-{n}", f"https://job-boards.greenhouse.io/fixturegh/jobs/{n}",
                     first_seen_at=_ago(hours=h)) for n, h in ages.items()]
    routes = {SM: rows}
    for n in ages:
        routes[f"/jobs/{n}?content=true"] = {"title": f"AI Engineer {n}", "content": "<p>Agents.</p>"}
    calls = []
    jobs = _run(routes, calls, max_details=2, gap_seconds=0.25)
    assert [j.raw_id for j in jobs] == ["gh-1", "gh-3"]
    assert len([u for u in calls if "boards-api" in u]) == 2
    assert _no_sleep == [0.25, 0.25]                             # a gap before each ATS call


def test_the_same_posting_listed_under_two_startups_is_looked_up_once():
    url = f"https://jobs.lever.co/fixturelv/{_uuid(5)}"
    rows = [_listing("lv-a", url, startup_id="fixturelv_cz", first_seen_at=_ago(hours=1)),
            _listing("lv-b", url, startup_id="otherlv_cz", first_seen_at=_ago(hours=2))]
    calls = []
    jobs = _run({SM: rows, "api.lever.co": {"text": "AI Engineer", "descriptionPlain": "Agents."}},
                calls, gap_seconds=0)
    assert [j.raw_id for j in jobs] == ["lv-a"]
    assert len([u for u in calls if "api.lever.co" in u]) == 1


# --- mapping helpers ---------------------------------------------------------------

@pytest.mark.parametrize("startup_id, text, expected", [
    ("tandem_health_se", "Tandem Health builds clinical AI. Join Tandem Health.", "Tandem Health"),
    ("elevenlabs_pl", "ABOUT ELEVENLABS. ElevenLabs is an AI audio company. ElevenLabs is remote.",
     "ElevenLabs"),
    ("n8n_de", "We build n8n.", "n8n"),
    ("pliant_de", "", "Pliant"),
    ("leyden_labs", "", "Leyden Labs"),                       # no country code: every word kept
    ("", "anything", ""),
])
def test_company_name_comes_from_startup_id_spelled_as_the_posting_spells_it(
        startup_id, text, expected):
    assert job_sources._startupmap_company(startup_id, text) == expected


def test_location_falls_back_to_listing_display_and_countries():
    row = {"location_display": "Berlin", "job_countries": ["Germany", "Spain"]}
    assert job_sources._startupmap_location("", row) == "Remote - Berlin (Germany, Spain)"
    row = {"location_display": "Germany", "job_countries": ["Germany"]}
    assert job_sources._startupmap_location(None, row) == "Remote - Germany"
    assert job_sources._startupmap_location("", {}) == "Remote"
    assert job_sources._startupmap_location("Remote, EU", row) == "Remote, EU"


def test_a_startupmap_posting_reaches_gate1_as_remote():
    """Neither the Ashby location nor the text says remote; StartupMap's flag,
    carried as the location prefix, is what Gate 1 reads."""
    from cv_tailor.gates import passes_gate1_tracks
    rows = [_listing("a", _ashby_url(1))]
    board = {"jobs": [_ashby_job(1, location="Europe",
                                 desc="<p>Own our AI automation stack end to end.</p>")]}
    [job] = _run({SM: rows, ASHBY_API: board}, gap_seconds=0)
    assert passes_gate1_tracks(job, {"ai": {"keywords": ["ai automation"]}}) == "ai"


def test_fetch_ashby_org_is_unchanged_unless_compensation_is_asked_for():
    """fetch_ashby_org feeds every ashby source and the ATS resolver; the
    compensation option must not leak into their requests or descriptions."""
    board = {"jobs": [_ashby_job(1, pay="€70K - €90K")]}
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=_router({ASHBY_API: board}, calls)):
        [plain] = job_sources.fetch_ashby_org("fixtureco", "FixtureCo")
        [paid] = job_sources.fetch_ashby_org("fixtureco", include_compensation=True)
    assert calls == [ASHBY_API + "fixtureco", ASHBY_API + "fixtureco?includeCompensation=true"]
    assert "Compensation" not in plain.description and plain.org == "FixtureCo"
    assert paid.description.endswith("Compensation: €70K - €90K")


# --- wiring: dispatch, sources.yaml, Gate 2 ------------------------------------------

def test_fetch_all_dispatches_startupmap_with_its_parameters():
    with mock.patch.object(job_sources, "fetch_startupmap", return_value=[]) as sm:
        job_sources.fetch_all([{"kind": "startupmap", "max_age_days": 2, "max_details": 10,
                                "gap_seconds": 0}])
        job_sources.fetch_all([{"kind": "startupmap"}])
    assert sm.call_args_list == [
        mock.call(max_age_days=2, max_details=10, gap_seconds=0),
        mock.call(max_age_days=3, max_details=60, gap_seconds=0.5),
    ]


def test_sources_yaml_registers_startupmap(capsys):
    """sources.yaml is only half the wiring -- fetch_all's board_dispatch IS
    the registration. Run the real entry through the real dispatch."""
    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "sources.yaml").read_text())
    entries = [s for s in cfg["sources"] if s.get("kind") == "startupmap"]
    assert len(entries) == 1
    with mock.patch("urllib.request.urlopen", side_effect=_router({SM: []})):
        assert job_sources.fetch_all(entries) == []
    out = capsys.readouterr()
    assert "unknown source kind" not in out.out + out.err
    assert "bad source config" not in out.err


def test_gate2_passes_startupmap_postings_as_startups(monkeypatch):
    from cv_tailor import enrich
    from cv_tailor.cache import connect

    monkeypatch.setattr(enrich, "hunter_headcount",
                        lambda *a, **k: pytest.fail("provenance must decide, not Hunter"))
    job = JobPosting(source="startupmap", org="FixtureCo", title="AI Engineer",
                     location="Remote - Europe", url="https://careers.fixtureco.example/ai",
                     description="x", raw_id="fixtureco_de-ai-engineer")
    conn = connect(":memory:")
    assert enrich.is_smb(job, conn) is True
    assert "startup" in enrich.smb_hint(job, conn)
    assert "StartupMap" in enrich.smb_hint(job, conn)
