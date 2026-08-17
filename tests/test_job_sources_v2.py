# tests/test_job_sources_v2.py
import io, json, sys, pathlib
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "boards"


def _fake_urlopen(payload):
    buf = io.BytesIO(json.dumps(payload).encode())
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    return cm


def _fake_urlopen_bytes(raw: bytes):
    buf = io.BytesIO(raw)
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    return cm


def _load_fixture_json(name):
    return json.loads((FIXTURES / name).read_text())


def _load_fixture_bytes(name):
    return (FIXTURES / name).read_bytes()


def test_greenhouse_maps_fields():
    payload = {"jobs": [{"id": 42, "title": "AI Engineer",
                         "location": {"name": "Remote - EU"},
                         "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                         "content": "&lt;p&gt;Build agents in Python&lt;/p&gt;"}]}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_greenhouse_org("acme", "Acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "greenhouse" and j.org == "Acme"
    assert j.title == "AI Engineer" and j.location == "Remote - EU"
    assert j.raw_id == "42"
    assert "Build agents in Python" in j.description  # entities + tags stripped


def test_lever_maps_fields():
    payload = [{"id": "abc", "text": "Backend Engineer",
                "categories": {"location": "Remote (Europe)", "commitment": "Full-time"},
                "hostedUrl": "https://jobs.lever.co/acme/abc",
                "descriptionPlain": "Python and TypeScript"}]
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_lever_org("acme", "Acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "lever" and j.title == "Backend Engineer"
    assert j.location == "Remote (Europe)" and j.raw_id == "abc"


def test_fetch_all_dispatches_new_kinds():
    payload_gh = {"jobs": []}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload_gh)):
        out = job_sources.fetch_all([{"kind": "greenhouse", "slug": "acme", "name": "Acme"}])
    assert out == []


# --- free board fetchers (ported from norina-jobs) ---

def test_fetch_remotive_maps_fields():
    payload = _load_fixture_json("remotive.json")
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_remotive("software-dev")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "remotive" and j.org == "Acme Remote"
    assert j.title == "AI Engineer"
    assert "Europe" in j.location
    assert j.url == "https://remotive.com/remote-jobs/software-dev/ai-engineer-555111"
    assert "Build agents in Python" in j.description
    assert j.raw_id == "555111"


def test_fetch_remotive_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        jobs = job_sources.fetch_remotive("marketing")
    assert jobs == []


def test_fetch_remoteok_skips_legal_notice_and_maps_fields():
    payload = _load_fixture_json("remoteok.json")
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_remoteok("marketing")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "remoteok" and j.org == "Widget Co"
    assert j.title == "Marketing Manager"
    assert "Portugal" in j.location
    assert j.url == "https://remoteok.com/remote-jobs/888222-marketing-manager-widget-co"
    assert "Own our content calendar" in j.description
    assert j.raw_id == "888222"


def test_fetch_remoteok_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        jobs = job_sources.fetch_remoteok("ai")
    assert jobs == []


def test_fetch_jobicy_maps_fields():
    payload = _load_fixture_json("jobicy.json")
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_jobicy(30, "copywriting")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "jobicy" and j.org == "Jobicy Client GmbH"
    assert j.title == "Content Copywriter"
    assert "Europe" in j.location
    assert j.url == "https://jobicy.com/jobs/777333-content-copywriter"
    assert "Write copy for landing pages" in j.description
    assert j.raw_id == "777333"


def test_fetch_jobicy_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        jobs = job_sources.fetch_jobicy(30, "software")
    assert jobs == []


def test_fetch_wwr_maps_fields_and_splits_org_from_title():
    raw = _load_fixture_bytes("wwr.xml")
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen_bytes(raw)):
        jobs = job_sources.fetch_wwr("programming")
    assert len(jobs) == 2
    j0 = jobs[0]
    assert j0.source == "wwr" and j0.org == "Acme Inc" and j0.title == "Backend Engineer"
    assert "Anywhere" in j0.location
    assert j0.url == "https://weworkremotely.com/remote-jobs/acme-inc-backend-engineer"
    assert "Build our API in Go" in j0.description
    assert j0.raw_id == "https://weworkremotely.com/remote-jobs/acme-inc-backend-engineer"
    # no colon in title -> org empty, full title kept, blank region falls back to Anywhere
    j1 = jobs[1]
    assert j1.org == "" and j1.title == "No Colon Title Job"
    assert "Anywhere" in j1.location


def test_fetch_wwr_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        jobs = job_sources.fetch_wwr("sales-and-marketing")
    assert jobs == []


def test_fetch_all_dispatches_board_kinds():
    remotive_payload = _load_fixture_json("remotive.json")
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(remotive_payload)):
        out = job_sources.fetch_all([{"kind": "remotive", "category": "software-dev"}])
    assert len(out) == 1 and out[0].source == "remotive"


def test_fetch_all_swallows_board_config_errors():
    # missing required key for the kind must not raise out of fetch_all (and must not
    # reach the network: the KeyError fires before any urlopen call)
    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not be called")):
        out = job_sources.fetch_all([{"kind": "remoteok"}, {"kind": "jobicy"}, {"kind": "wwr"}])
    assert out == []


def test_remote_boards_gate_includes_all_four_board_sources():
    from cv_tailor.enrich import REMOTE_BOARDS
    assert {"remotive", "remoteok", "jobicy", "wwr"} <= REMOTE_BOARDS


# --- arbeitnow + himalayas (keyless EU/remote aggregators, ported htgaj) -----

def _url_router(routes, calls=None):
    """urlopen side_effect that picks a payload by substring of the requested
    URL and records every URL, so a paginating fetcher's requests can be
    asserted without any network access. An unrouted URL is a test failure,
    never a silent empty page."""
    def _open(req, *a, **kw):
        url = getattr(req, "full_url", None) or str(req)
        if calls is not None:
            calls.append(url)
        for fragment, payload in routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return _fake_urlopen(payload)
        raise AssertionError(f"unexpected url {url}")
    return _open


def test_fetch_arbeitnow_maps_fields():
    payload = _load_fixture_json("arbeitnow.json")
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=_url_router({"page=1": payload}, calls)):
        jobs = job_sources.fetch_arbeitnow(1)
    assert len(calls) == 1 and "arbeitnow.com/api/job-board-api" in calls[0]
    # the id-less 4th posting is dropped; the other three map
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source == "arbeitnow" and j.org == "Fixture Remote GmbH"
    assert j.title == "AI Engineer"
    assert "Remote" in j.location and "Europe" in j.location and "Berlin" in j.location
    assert j.url == "https://www.arbeitnow.com/jobs/companies/fixture-remote/ai-engineer-berlin-101"
    assert "Build agents in Python" in j.description  # tags stripped
    assert j.raw_id == "ai-engineer-berlin-101"
    # no apply link distinct from the posting url -> the default, never invented
    assert j.apply_options == []


def test_fetch_arbeitnow_does_not_fabricate_remoteness():
    """Only ~6% of arbeitnow postings are remote and the API ignores its own
    ?remote=true filter, so the mapping must report the per-posting flag: an
    onsite row must fail gates.is_remote instead of being smuggled past Gate 1
    by a blanket 'Remote - ' prefix."""
    from cv_tailor.gates import is_remote
    payload = _load_fixture_json("arbeitnow.json")
    with mock.patch("urllib.request.urlopen", side_effect=_url_router({"page=1": payload})):
        jobs = job_sources.fetch_arbeitnow(1)
    by_title = {j.title: j for j in jobs}
    onsite = by_title["Service Technician"]
    assert is_remote(onsite.location, onsite.description) is False
    remote = by_title["AI Engineer"]
    assert is_remote(remote.location, remote.description) is True


def test_fetch_arbeitnow_never_emits_an_empty_raw_id():
    payload = _load_fixture_json("arbeitnow.json")
    with mock.patch("urllib.request.urlopen", side_effect=_url_router({"page=1": payload})):
        jobs = job_sources.fetch_arbeitnow(1)
    assert all(j.raw_id for j in jobs)
    # blank slug falls back to the posting url...
    slugless = next(j for j in jobs if j.org == "Fixture Slugless Ltd")
    assert slugless.raw_id == slugless.url
    # ...and a posting with neither is dropped rather than stored blank
    assert "Fixture Idless Ltd" not in {j.org for j in jobs}


def test_fetch_arbeitnow_paginates_and_dedupes():
    page1 = {"data": [{"slug": "a-1", "title": "A", "company_name": "One",
                       "url": "https://www.arbeitnow.com/jobs/a-1", "remote": True,
                       "location": "Berlin", "description": "<p>x</p>"}]}
    page2 = {"data": [{"slug": "a-1", "title": "A dup", "company_name": "One",
                       "url": "https://www.arbeitnow.com/jobs/a-1", "remote": True,
                       "location": "Berlin", "description": "<p>x</p>"},
                      {"slug": "b-2", "title": "B", "company_name": "Two",
                       "url": "https://www.arbeitnow.com/jobs/b-2", "remote": True,
                       "location": "Lisbon", "description": "<p>y</p>"}]}
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"page=1": page1, "page=2": page2}, calls)):
        jobs = job_sources.fetch_arbeitnow(2)
    assert len(calls) == 2
    assert [j.raw_id for j in jobs] == ["a-1", "b-2"]


def test_fetch_arbeitnow_stops_at_the_first_empty_page():
    page1 = {"data": [{"slug": "a-1", "title": "A", "company_name": "One",
                       "url": "https://www.arbeitnow.com/jobs/a-1", "remote": True,
                       "location": "Berlin", "description": "<p>x</p>"}]}
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"page=1": page1, "page=2": {"data": []},
                                             "page=3": {"data": []}}, calls)):
        jobs = job_sources.fetch_arbeitnow(3)
    assert len(calls) == 2  # never asks for page 3 after an empty page 2
    assert len(jobs) == 1


def test_fetch_arbeitnow_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        jobs = job_sources.fetch_arbeitnow(2)
    assert jobs == []


def test_fetch_arbeitnow_keeps_pages_fetched_before_a_mid_run_failure():
    page1 = {"data": [{"slug": "a-1", "title": "A", "company_name": "One",
                       "url": "https://www.arbeitnow.com/jobs/a-1", "remote": True,
                       "location": "Berlin", "description": "<p>x</p>"}]}
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"page=1": page1,
                                             "page=2": RuntimeError("boom")})):
        jobs = job_sources.fetch_arbeitnow(2)
    assert [j.raw_id for j in jobs] == ["a-1"]


def test_fetch_himalayas_maps_fields():
    payload = _load_fixture_json("himalayas.json")
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=_url_router({"offset=0": payload}, calls)):
        jobs = job_sources.fetch_himalayas(20)
    assert len(calls) == 1 and "himalayas.app/jobs/api" in calls[0] and "limit=20" in calls[0]
    # the guid-less, link-less 3rd posting is dropped; the other two map
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "himalayas" and j.org == "Fixture Himalaya BV"
    assert j.title == "Machine Learning Engineer"
    assert j.location == "Remote - Germany, Netherlands"
    assert j.url == "https://himalayas.app/companies/fixture-himalaya/jobs/machine-learning-engineer"
    assert "Ship models to production" in j.description
    assert j.raw_id == "https://himalayas.app/companies/fixture-himalaya/jobs/machine-learning-engineer"
    assert j.apply_options == []


def test_fetch_himalayas_never_emits_an_empty_raw_id():
    """The htgaj adapter keyed on `id or slug`; this API ships NEITHER field,
    so that chain would have blanked the raw_id of every posting -- and one
    blank raw_id makes cache.is_new read every later blank one from the same
    source as already-seen."""
    payload = _load_fixture_json("himalayas.json")
    with mock.patch("urllib.request.urlopen", side_effect=_url_router({"offset=0": payload})):
        jobs = job_sources.fetch_himalayas(20)
    assert all(j.raw_id for j in jobs)
    guidless = next(j for j in jobs if j.org == "Fixture Guidless Inc")
    assert guidless.raw_id == "https://himalayas.app/companies/fixture-guidless/jobs/technical-writer"
    assert guidless.location == "Remote - Anywhere"  # empty restrictions
    assert "Fixture Idless Inc" not in {j.org for j in jobs}


def test_fetch_himalayas_pages_by_offset_and_dedupes():
    def _job(guid, title):
        return {"title": title, "companyName": "Co", "locationRestrictions": ["Germany"],
                "description": "<p>x</p>", "guid": guid, "applicationLink": guid}
    page1 = {"jobs": [_job(f"https://himalayas.app/jobs/{i}", f"J{i}") for i in range(20)]}
    page2 = {"jobs": [_job("https://himalayas.app/jobs/0", "J0 dup"),
                      _job("https://himalayas.app/jobs/99", "J99")]}
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"offset=0": page1, "offset=20": page2}, calls)):
        jobs = job_sources.fetch_himalayas(40)
    assert len(calls) == 2
    assert len(jobs) == 21  # 20 + 1 new, the duplicate guid collapses
    assert len({j.raw_id for j in jobs}) == 21


def test_fetch_himalayas_stops_when_a_page_comes_back_short():
    page1 = {"jobs": [{"title": "J", "companyName": "Co", "locationRestrictions": [],
                       "description": "<p>x</p>",
                       "guid": "https://himalayas.app/jobs/1"}]}
    calls = []
    with mock.patch("urllib.request.urlopen", side_effect=_url_router({"offset=0": page1}, calls)):
        jobs = job_sources.fetch_himalayas(60)
    assert len(calls) == 1  # a short page means the board is exhausted
    assert len(jobs) == 1


def test_fetch_himalayas_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        jobs = job_sources.fetch_himalayas(40)
    assert jobs == []


def test_fetch_all_dispatches_the_two_new_board_kinds():
    arbeitnow = _load_fixture_json("arbeitnow.json")
    himalayas = _load_fixture_json("himalayas.json")
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"arbeitnow.com": arbeitnow,
                                             "himalayas.app": himalayas})):
        out = job_sources.fetch_all([{"kind": "arbeitnow", "pages": 1},
                                     {"kind": "himalayas", "count": 20}])
    assert {j.source for j in out} == {"arbeitnow", "himalayas"}
    assert all(j.raw_id for j in out)


def test_sources_yaml_registers_the_two_new_boards(capsys):
    """sources.yaml is only half the wiring -- fetch_all's board_dispatch IS
    the registration, so an entry whose kind nothing dispatches is silently
    skipped. Run the real yaml entries through the real dispatch."""
    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "sources.yaml").read_text())
    entries = [s for s in cfg["sources"] if s.get("kind") in ("arbeitnow", "himalayas")]
    assert {s["kind"] for s in entries} == {"arbeitnow", "himalayas"}
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"arbeitnow.com": {"data": []},
                                             "himalayas.app": {"jobs": []}})):
        out = job_sources.fetch_all(entries)
    assert out == []
    assert "unknown source kind" not in capsys.readouterr().out


def test_arbeitnow_posting_url_is_not_mistaken_for_a_company_domain():
    """Gate 2 asks Hunter for a headcount whenever the posting URL looks like a
    company domain. Every arbeitnow posting lives on arbeitnow.com, so without
    the board listed there, one cached verdict for 'arbeitnow.com' would be
    applied to every arbeitnow job."""
    from cv_tailor.enrich import company_domain
    job = job_sources.JobPosting(
        source="arbeitnow", org="Fixture Remote GmbH", title="AI Engineer",
        location="Remote - Europe (Berlin)",
        url="https://www.arbeitnow.com/jobs/companies/fixture-remote/ai-engineer-berlin-101",
        description="", raw_id="ai-engineer-berlin-101")
    assert company_domain(job) is None


# --- adzuna (credentialed, ten EU markets) -----------------------------------

def test_fetch_adzuna_no_ops_without_credentials(monkeypatch, capsys):
    """The state this ships in: neither var is in .env yet. It must warn and
    return [] without ever touching the network -- the fetch_serpapi contract."""
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    with mock.patch("urllib.request.urlopen",
                    side_effect=AssertionError("must not be called")) as urlopen:
        out = job_sources.fetch_adzuna("AI engineer remote")
    urlopen.assert_not_called()
    assert out == []
    assert "ADZUNA_APP_ID" in capsys.readouterr().out


def test_fetch_adzuna_no_ops_when_only_one_of_the_two_vars_is_set(monkeypatch, capsys):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    with mock.patch("urllib.request.urlopen",
                    side_effect=AssertionError("must not be called")):
        assert job_sources.fetch_adzuna("AI engineer remote") == []
    assert "ADZUNA_APP_KEY" in capsys.readouterr().out


def test_fetch_adzuna_maps_fields():
    payload = _load_fixture_json("adzuna.json")
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"/jobs/de/search/1": payload}, calls)):
        jobs = job_sources.fetch_adzuna("AI engineer remote", country="de",
                                        app_id="id", app_key="key")
    assert len(calls) == 1
    assert "api.adzuna.com/v1/api/jobs/de/search/1" in calls[0]
    # the id-less-and-link-less 4th posting is dropped; the other three map
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source == "adzuna" and j.org == "Fixture Adzuna GmbH"
    assert j.title == "AI Engineer - Remote"
    assert j.raw_id == "9000000001"
    assert "Build agents in Python" in j.description
    # redirect_url IS the posting url -- no distinct apply link to promote
    assert j.apply_options == []


def test_fetch_adzuna_never_puts_the_app_id_in_the_stored_url():
    """MEASURED 2026-08-17: every market's redirect_url carries
    `utm_source=<ADZUNA_APP_ID>`. job.url is persisted to the queue JSON, to
    scans/<date>.json, to the Telegram digest and rendered as an <a href> on
    /scout, so keeping the query string would write a live credential into all
    four. Stripping it is safe: the bare /jobs/details/<id> path behaves
    identically (both are bot-walled 403 to a script, both open in a browser)."""
    payload = _load_fixture_json("adzuna.json")
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"/jobs/de/search/1": payload})):
        jobs = job_sources.fetch_adzuna("AI engineer remote", country="de",
                                        app_id="id", app_key="key")
    assert jobs
    for j in jobs:
        assert "utm_source" not in j.url and "?" not in j.url
    assert jobs[0].url == "https://www.adzuna.de/jobs/details/9000000001"


def test_fetch_adzuna_gives_gate1_an_english_country_name():
    """MEASURED: location.area[0] is the LOCAL-LANGUAGE country name
    ('Deutschland', 'Nederland', 'Polska'), and gates._EU_RE only knows the
    English ones -- so a German remote posting would fail is_eu_eligible with
    the API's own location text. The market's English name is added from the
    country code the query was issued against."""
    from cv_tailor.gates import is_eu_eligible
    payload = _load_fixture_json("adzuna.json")
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"/jobs/de/search/1": payload})):
        jobs = job_sources.fetch_adzuna("AI engineer remote", country="de",
                                        app_id="id", app_key="key")
    j = jobs[0]
    assert "Germany" in j.location and "München" in j.location
    assert is_eu_eligible(j.location, j.description) is True
    # the raw API location alone would NOT have passed
    assert is_eu_eligible("München, München (Kreis)", "") is False


def test_fetch_adzuna_does_not_fabricate_remoteness():
    """Adzuna exposes NO remote flag (verified by field census over 200 live
    postings), and `what=... remote` is a full-text AND term, not a work-mode
    filter. So remoteness is REPORTED from the posting's own title+description
    via the same predicate Gate 1 applies -- the prefix only promotes evidence
    Gate 1 cannot see (it never reads the title), it never invents any."""
    from cv_tailor.gates import is_remote
    payload = _load_fixture_json("adzuna.json")
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"/jobs/de/search/1": payload})):
        jobs = job_sources.fetch_adzuna("AI engineer remote", country="de",
                                        app_id="id", app_key="key")
    by_title = {j.title: j for j in jobs}
    onsite = by_title["Service Technician"]
    assert not onsite.location.startswith("Remote")
    assert is_remote(onsite.location, onsite.description) is False
    # title-only remote evidence reaches Gate 1, which never reads the title
    remote = by_title["AI Engineer - Remote"]
    assert remote.location.startswith("Remote - ")
    assert is_remote(remote.location, "") is True


def test_fetch_adzuna_never_emits_an_empty_raw_id():
    payload = _load_fixture_json("adzuna.json")
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"/jobs/de/search/1": payload})):
        jobs = job_sources.fetch_adzuna("AI engineer remote", country="de",
                                        app_id="id", app_key="key")
    assert all(j.raw_id for j in jobs)
    # no id -> the (stripped) posting url, which is still stable per posting
    idless = next(j for j in jobs if j.org == "Fixture Idless GmbH")
    assert idless.raw_id == "https://www.adzuna.de/jobs/details/9000000003"
    # neither -> dropped rather than stored blank
    assert "Fixture Ghost GmbH" not in {j.org for j in jobs}


def test_fetch_adzuna_sends_the_work_mode_in_what_and_never_in_where():
    """MEASURED 2026-08-17: `where=remote` returns count=0 -- `where` expects a
    PLACE, so a work mode there looks exactly like a dead credential."""
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"adzuna.com": {"count": 0, "results": []}}, calls)):
        job_sources.fetch_adzuna("AI engineer remote", country="gb",
                                 app_id="id", app_key="key")
    assert len(calls) == 1
    assert "what=AI+engineer+remote" in calls[0]
    assert "where=" not in calls[0]


def test_fetch_adzuna_requests_fresh_postings_first():
    """A daily scan wants NEW postings; live `created` dates run back 9 months
    on an unsorted query. sort_by=date + max_days_old are both supported
    (measured: 1304 -> 219 results with max_days_old=7)."""
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"adzuna.com": {"count": 0, "results": []}}, calls)):
        job_sources.fetch_adzuna("AI engineer remote", country="gb", max_days_old=7,
                                 app_id="id", app_key="key")
    assert "sort_by=date" in calls[0] and "max_days_old=7" in calls[0]


def test_fetch_adzuna_refuses_an_unsupported_market(capsys):
    """The country code goes into the URL PATH. Adzuna also serves us/au/br/in,
    so a typo (or 'us') would silently pull non-EU jobs into an EU-only scan --
    and an arbitrary string would be path-injected into the request URL."""
    for bad in ("us", "", "gb/../us", "GB "):
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("must not be called")):
            assert job_sources.fetch_adzuna("q", country=bad, app_id="id", app_key="key") == []
    assert "unsupported adzuna country" in capsys.readouterr().out


def test_fetch_adzuna_caps_results_per_page_at_the_api_ceiling():
    """MEASURED: results_per_page=100 still returns 50."""
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"adzuna.com": {"count": 0, "results": []}}, calls)):
        job_sources.fetch_adzuna("q", country="gb", results_per_page=500,
                                 app_id="id", app_key="key")
    assert "results_per_page=50" in calls[0]


def test_fetch_adzuna_paginates_and_dedupes():
    def _row(i):
        return {"id": str(i), "title": "Remote Engineer",
                "company": {"display_name": "Co"},
                "location": {"display_name": "London", "area": ["UK", "London"]},
                "description": "Remote work.",
                "redirect_url": f"https://www.adzuna.co.uk/jobs/details/{i}?utm_source=id"}
    page1 = {"count": 3, "results": [_row(1), _row(2)]}
    page2 = {"count": 3, "results": [_row(2), _row(3)]}
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"/search/1": page1, "/search/2": page2}, calls)):
        jobs = job_sources.fetch_adzuna("q", country="gb", pages=2,
                                        app_id="id", app_key="key")
    assert len(calls) == 2
    assert [j.raw_id for j in jobs] == ["1", "2", "3"]


def test_fetch_adzuna_stops_at_the_first_empty_page():
    calls = []
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"/search/1": {"count": 0, "results": []}}, calls)):
        jobs = job_sources.fetch_adzuna("q", country="gb", pages=3,
                                       app_id="id", app_key="key")
    assert len(calls) == 1 and jobs == []


def test_fetch_adzuna_swallows_errors():
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        assert job_sources.fetch_adzuna("q", country="gb", app_id="id", app_key="key") == []


def test_fetch_adzuna_error_warning_never_echoes_the_credentials(capsys):
    """A urllib error stringifies the URL it failed on. That URL carries
    app_id AND app_key, so a raw `{e}` in the warning would print both into
    scans/logs/<date>.log."""
    url = ("https://api.adzuna.com/v1/api/jobs/gb/search/1?app_id=SECRETID"
           "&app_key=SECRETKEY&what=q")
    with mock.patch("urllib.request.urlopen",
                    side_effect=RuntimeError(f"HTTP Error 401 for {url}")):
        job_sources.fetch_adzuna("q", country="gb", app_id="SECRETID", app_key="SECRETKEY")
    printed = capsys.readouterr().out
    assert "SECRETID" not in printed and "SECRETKEY" not in printed
    assert "adzuna fetch failed" in printed


def test_fetch_all_dispatches_adzuna(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    payload = _load_fixture_json("adzuna.json")
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"adzuna.com": payload})):
        out = job_sources.fetch_all([{"kind": "adzuna", "query": "AI engineer remote",
                                      "country": "de"}])
    assert {j.source for j in out} == {"adzuna"}
    assert all(j.raw_id for j in out)


def test_fetch_all_never_takes_adzuna_credentials_from_a_source_entry(monkeypatch, capsys):
    """A source entry lives in the COMMITTED sources.yaml. If the dispatch read
    app_id/app_key from it, the next person needing Adzuna would put live
    credentials in a tracked file (SEC020). Env is the only channel."""
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    with mock.patch("urllib.request.urlopen",
                    side_effect=AssertionError("must not be called")):
        out = job_sources.fetch_all([{"kind": "adzuna", "query": "q", "country": "de",
                                      "app_id": "leaked", "app_key": "leaked"}])
    assert out == []
    assert "ADZUNA_APP_ID" in capsys.readouterr().out


def test_adzuna_posting_url_is_not_mistaken_for_a_company_domain():
    """Every Adzuna posting url lives on a per-country adzuna TLD (measured:
    adzuna.at/.be/.ch/.de/.es/.fr/.co.uk/.it/.nl/.pl). Without all ten listed
    as job boards, ONE cached Hunter verdict for 'adzuna.de' would stand in for
    every German Adzuna job -- the arbeitnow.com failure mode, ten times over."""
    from cv_tailor.enrich import company_domain
    for host in ("www.adzuna.at", "www.adzuna.be", "www.adzuna.ch", "www.adzuna.de",
                 "www.adzuna.es", "www.adzuna.fr", "www.adzuna.co.uk", "www.adzuna.it",
                 "www.adzuna.nl", "www.adzuna.pl"):
        job = job_sources.JobPosting(
            source="adzuna", org="Fixture Adzuna GmbH", title="AI Engineer",
            location="Remote - Germany", url=f"https://{host}/jobs/details/9000000001",
            description="", raw_id="9000000001")
        assert company_domain(job) is None, host


def test_sources_yaml_registers_adzuna(capsys):
    """sources.yaml is only half the wiring -- fetch_all's board_dispatch IS the
    registration. Run the real yaml entries through the real dispatch."""
    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "sources.yaml").read_text())
    entries = [s for s in cfg["sources"] if s.get("kind") == "adzuna"]
    assert entries, "no adzuna entries in sources.yaml"
    # every entry names a supported EU market and keeps the work mode out of `where`
    for s in entries:
        assert s["country"] in job_sources.ADZUNA_MARKETS
        assert "where" not in s
    with mock.patch("urllib.request.urlopen",
                    side_effect=_url_router({"adzuna.com": {"count": 0, "results": []}})):
        out = job_sources.fetch_all(entries)
    assert out == []
    assert "unknown source kind" not in capsys.readouterr().out


def test_sources_yaml_carries_no_credentials():
    """sources.yaml is git-TRACKED and this repo is PUBLIC. No source entry may
    carry a key of its own, whatever the source kind -- credentials reach the
    fetchers through the environment only."""
    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "sources.yaml").read_text())
    banned = ("app_id", "app_key", "api_key", "api_keys", "key", "token", "secret")
    offenders = [(s.get("kind"), k) for s in cfg["sources"] for k in s if k in banned]
    assert offenders == []


def test_env_example_documents_the_adzuna_variable_names():
    """Teodor adds the real values later; the NAMES have to be discoverable."""
    text = (pathlib.Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert "ADZUNA_APP_ID=" in text and "ADZUNA_APP_KEY=" in text
    # documented, never filled in
    for line in text.splitlines():
        if line.startswith(("ADZUNA_APP_ID=", "ADZUNA_APP_KEY=")):
            assert line.split("=", 1)[1] == ""


# --- jsearch (credentialed, UK-targeted, budget-aware) -----------------------

def _kv_router(routes, calls=None):
    """Like _url_router but also records each request's headers, so key
    rotation and the x-api-key wiring can be asserted with no network access.
    A routed value that is an Exception is raised (used for HTTPError 401/429)."""
    def _open(req, *a, **kw):
        url = getattr(req, "full_url", None) or str(req)
        if calls is not None:
            calls.append((url, dict(getattr(req, "headers", {}) or {})))
        for fragment, payload in routes.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, list):          # one payload per attempt
                    nxt = payload.pop(0)
                    if isinstance(nxt, Exception):
                        raise nxt
                    return _fake_urlopen(nxt)
                return _fake_urlopen(payload)
        raise AssertionError(f"unexpected url {url}")
    return _open


def _http_error(code):
    import urllib.error
    return urllib.error.HTTPError(
        "https://api.openwebninja.com/jsearch/search", code, "boom", {}, None)


def _api_key_of(headers):
    """urllib.request.Request.add_header capitalizes header NAMES, so the sent
    header is 'X-api-key'. Verified live: the API accepts it (HTTP 200 through
    urllib), so the config file's 'must be lowercase x-api-key' note does not
    apply to this transport."""
    return next(v for k, v in headers.items() if k.lower() == "x-api-key")


def test_fetch_jsearch_no_ops_without_keys(monkeypatch, capsys):
    """The state this ships in: JSEARCH_API_KEYS is not in .env yet."""
    monkeypatch.delenv("JSEARCH_API_KEYS", raising=False)
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=AssertionError("must not be called")) as urlopen:
        out = job_sources.fetch_jsearch("AI engineer remote UK")
    urlopen.assert_not_called()
    assert out == []
    assert "JSEARCH_API_KEYS" in capsys.readouterr().out


def test_fetch_jsearch_checks_keys_before_touching_the_budget(monkeypatch, tmp_path):
    """The fetch_serpapi ordering: an unset credential must not burn a budget
    slot on a request that was never going to reach the network."""
    from cv_tailor.budget import JSearchBudget
    monkeypatch.delenv("JSEARCH_API_KEYS", raising=False)
    job_sources.reset_jsearch_key_rotation()
    budget = JSearchBudget(path=tmp_path / "jsearch_budget.json", monthly_cap=5)
    with mock.patch("urllib.request.urlopen",
                    side_effect=AssertionError("must not be called")):
        assert job_sources.fetch_jsearch("q", budget=budget) == []
    assert budget.used() == 0


def test_fetch_jsearch_maps_fields():
    payload = _load_fixture_json("jsearch.json")
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload}, calls)):
        jobs = job_sources.fetch_jsearch("AI engineer remote UK", api_keys=["k1"])
    assert len(calls) == 1
    url, headers = calls[0]
    assert "api.openwebninja.com/jsearch/search" in url
    assert "query=AI+engineer+remote+UK" in url and "num_pages=1" in url and "page=1" in url
    assert "country=gb" in url
    assert _api_key_of(headers) == "k1"
    # the id-less, uid-less, link-less 4th posting is dropped; the other three map
    assert len(jobs) == 3
    j = jobs[0]
    assert j.source == "jsearch" and j.org == "Fixture JSearch Ltd"
    assert j.title == "AI Engineer"
    assert j.raw_id == "Rml4dHVyZUpvYklkT25l"
    assert j.url == "https://fixture-jsearch.example/careers/ai-engineer"
    assert "Build agents in Python" in j.description


def test_fetch_jsearch_does_not_build_location_from_the_null_city_fields():
    """MEASURED over a full live page: job_city, job_state AND job_country are
    null on every row. The htgaj adapter builds its location from exactly those
    three, so it would emit "" for every posting -- and Gate 1 would then have
    no geo signal at all to judge. job_location is the real field, and the
    market's English name is appended so gates._EU_RE has something to match
    ("Anywhere" alone passes only via the global rule; "Manchester" would not
    pass at all)."""
    from cv_tailor.gates import is_eu_eligible
    payload = _load_fixture_json("jsearch.json")
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload})):
        jobs = job_sources.fetch_jsearch("AI engineer remote UK", api_keys=["k1"])
    by_title = {j.title: j for j in jobs}
    assert "United Kingdom" in by_title["AI Engineer"].location
    onsite = by_title["Onsite Support Engineer"]
    assert "Manchester" in onsite.location and "United Kingdom" in onsite.location
    assert is_eu_eligible(onsite.location, onsite.description) is True
    assert is_eu_eligible("Manchester", "") is False   # the raw field alone fails
    # a null job_location still yields the market, never an empty string
    assert by_title["Link Only Role"].location == "Remote - United Kingdom"


def test_fetch_jsearch_reports_the_remote_flag_it_is_given():
    from cv_tailor.gates import is_remote
    payload = _load_fixture_json("jsearch.json")
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload})):
        jobs = job_sources.fetch_jsearch("AI engineer remote UK", api_keys=["k1"])
    by_title = {j.title: j for j in jobs}
    remote = by_title["AI Engineer"]
    assert remote.location.startswith("Remote - ")
    assert is_remote(remote.location, "") is True
    onsite = by_title["Onsite Support Engineer"]
    assert not onsite.location.startswith("Remote")
    assert is_remote(onsite.location, onsite.description) is False


def test_fetch_jsearch_normalizes_apply_options_into_this_repos_shape():
    """jsearch entries are {apply_link, is_direct, publisher}; SerpAPI's are
    {link, title}. _apply_option_links reads link/title, so without the remap
    every option would be silently dropped -- and /scout would show a human no
    apply link at all."""
    payload = _load_fixture_json("jsearch.json")
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload})):
        jobs = job_sources.fetch_jsearch("AI engineer remote UK", api_keys=["k1"])
    opts = jobs[0].apply_options
    assert [o["url"] for o in opts] == [
        "https://fixture-jsearch.example/careers/ai-engineer",
        "https://fixtureboard.example/jobs/ai-engineer"]          # javascript: dropped
    assert opts[0]["label"] == "Fixture Careers"


def test_fetch_jsearch_never_navigates_to_a_google_serp():
    """The htgaj adapter falls back to job_google_link. That is a google.com
    search URL -- useless to a portal adapter, and adapter_for would have to
    reject it anyway. A posting with no real apply link is dropped instead."""
    payload = _load_fixture_json("jsearch.json")
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload})):
        jobs = job_sources.fetch_jsearch("AI engineer remote UK", api_keys=["k1"])
    assert all("google.com" not in j.url for j in jobs)
    assert "Fixture Ghost Ltd" not in {j.org for j in jobs}


def test_fetch_jsearch_never_emits_an_empty_raw_id():
    payload = _load_fixture_json("jsearch.json")
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload})):
        jobs = job_sources.fetch_jsearch("AI engineer remote UK", api_keys=["k1"])
    assert all(j.raw_id for j in jobs)
    # no job_id -> job_uid
    assert next(j for j in jobs if j.org == "Fixture Onsite Ltd").raw_id == "Rml4dHVyZVVpZFR3bw=="
    # neither -> the apply link
    assert (next(j for j in jobs if j.org == "Fixture Linkonly Ltd").raw_id
            == "https://fixtureboard.example/jobs/link-only-role")


def test_fetch_jsearch_rotates_across_keys_on_successive_queries():
    """Six keys at 200 req/mo each. Spending one key down while five sit idle
    is how the htgaj run burned four of them; round-robin spreads the load."""
    payload = {"status": "OK", "data": []}
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload}, calls)):
        for _ in range(4):
            job_sources.fetch_jsearch("q", api_keys=["k1", "k2", "k3"])
    assert [_api_key_of(h) for _, h in calls] == ["k1", "k2", "k3", "k1"]


def test_fetch_jsearch_retries_the_next_key_when_one_is_rejected():
    """MEASURED: an invalid or spent key answers HTTP 401 -- urllib raises, so
    the htgaj adapter's `data["status"] == "FAIL"` branch could never fire and
    the query would just be lost."""
    payload = _load_fixture_json("jsearch.json")
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": [_http_error(401), payload]},
                                           calls)):
        jobs = job_sources.fetch_jsearch("q", api_keys=["k1", "k2"])
    assert [_api_key_of(h) for _, h in calls] == ["k1", "k2"]
    assert len(jobs) == 3


def test_fetch_jsearch_does_not_retry_a_key_already_known_dead():
    payload = {"status": "OK", "data": []}
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": [_http_error(429), payload,
                                                                 payload]}, calls)):
        job_sources.fetch_jsearch("q1", api_keys=["k1", "k2"])   # k1 dies, k2 answers
        job_sources.fetch_jsearch("q2", api_keys=["k1", "k2"])   # k1 must be skipped
    assert [_api_key_of(h) for _, h in calls] == ["k1", "k2", "k2"]


def test_fetch_jsearch_gives_up_when_every_key_is_rejected(capsys):
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": [_http_error(401),
                                                                 _http_error(401)]}, calls)):
        out = job_sources.fetch_jsearch("q", api_keys=["k1", "k2"])
    assert out == [] and len(calls) == 2
    assert "no usable jsearch key" in capsys.readouterr().out


def test_fetch_jsearch_error_warning_never_echoes_a_key(capsys):
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=RuntimeError("failed with x-api-key: SECRETKEY")):
        job_sources.fetch_jsearch("q", api_keys=["SECRETKEY"])
    printed = capsys.readouterr().out
    assert "SECRETKEY" not in printed
    assert "jsearch fetch failed" in printed


def test_fetch_jsearch_reads_keys_from_the_env_var(monkeypatch):
    monkeypatch.setenv("JSEARCH_API_KEYS", " ka , kb ,, ")
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": {"status": "OK", "data": []}},
                                           calls)):
        job_sources.fetch_jsearch("q")
        job_sources.fetch_jsearch("q")
    assert [_api_key_of(h) for _, h in calls] == ["ka", "kb"]


def test_fetch_jsearch_never_reads_the_committed_key_file(monkeypatch):
    """~/clawd/config/jsearch_api_keys.json is tracked in git. Reading it from
    code is a live SEC020/SEC022 finding, so there is no file fallback at all."""
    import inspect
    src = inspect.getsource(job_sources)
    assert "jsearch_api_keys" not in src and "clawd/config" not in src
    monkeypatch.delenv("JSEARCH_API_KEYS", raising=False)
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("pathlib.Path.read_text",
                    side_effect=AssertionError("must not read any file")):
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("must not be called")):
            assert job_sources.fetch_jsearch("q") == []


def test_fetch_jsearch_consults_budget_and_skips_network_when_exhausted(tmp_path, capsys):
    from cv_tailor.budget import JSearchBudget
    budget = JSearchBudget(path=tmp_path / "jsearch_budget.json", monthly_cap=0)
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen") as urlopen:
        out = job_sources.fetch_jsearch("AI engineer remote UK", api_keys=["k1"],
                                        budget=budget)
    urlopen.assert_not_called()
    assert out == []
    printed = capsys.readouterr().out
    assert "jsearch budget exhausted" in printed and "AI engineer remote UK" in printed


def test_fetch_jsearch_takes_one_budget_unit_per_request_including_retries(tmp_path):
    """A rejected key still cost a real HTTP request against the shared
    1200/mo pool, so the counter must not undercount retries."""
    from cv_tailor.budget import JSearchBudget
    budget = JSearchBudget(path=tmp_path / "jsearch_budget.json", monthly_cap=5)
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": [_http_error(401),
                                                                 {"status": "OK", "data": []}]})):
        job_sources.fetch_jsearch("q", api_keys=["k1", "k2"], budget=budget)
    assert budget.used() == 2


def test_fetch_jsearch_stops_retrying_when_the_budget_runs_out_mid_query(tmp_path, capsys):
    from cv_tailor.budget import JSearchBudget
    budget = JSearchBudget(path=tmp_path / "jsearch_budget.json", monthly_cap=1)
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": [_http_error(401)]}, calls)):
        out = job_sources.fetch_jsearch("q", api_keys=["k1", "k2"], budget=budget)
    assert out == [] and len(calls) == 1
    assert budget.used() == 1


def test_fetch_jsearch_refuses_a_non_uk_market(capsys):
    """MEASURED: `country=gb` is echoed back but returns 0 rows on its own,
    'python developer remote germany' returns 0, and 'AI engineer remote
    europe' returns US employers. Continental queries are pure budget waste at
    200 req/mo per key, so anything outside the market map is refused."""
    for bad in ("de", "us", "", "gb&country=us"):
        job_sources.reset_jsearch_key_rotation()
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("must not be called")):
            assert job_sources.fetch_jsearch("q", country=bad, api_keys=["k1"]) == []
    assert "unsupported jsearch country" in capsys.readouterr().out


def test_fetch_jsearch_swallows_errors():
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
        assert job_sources.fetch_jsearch("q", api_keys=["k1"]) == []


def test_fetch_all_dispatches_jsearch_with_its_own_budget(monkeypatch, tmp_path):
    from cv_tailor.budget import JSearchBudget
    monkeypatch.setenv("JSEARCH_API_KEYS", "k1")
    budget = JSearchBudget(path=tmp_path / "jsearch_budget.json", monthly_cap=5)
    payload = _load_fixture_json("jsearch.json")
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": payload})):
        out = job_sources.fetch_all([{"kind": "jsearch", "query": "AI engineer remote UK"}],
                                    jsearch_budget=budget)
    assert {j.source for j in out} == {"jsearch"}
    assert all(j.raw_id for j in out)
    assert budget.used() == 1


def test_fetch_all_threads_one_shared_jsearch_budget_across_queries(monkeypatch, tmp_path):
    """One instance for the whole scan, like serp_budget -- otherwise each
    source re-reads a stale count and the cap never binds."""
    from cv_tailor.budget import JSearchBudget
    monkeypatch.setenv("JSEARCH_API_KEYS", "k1,k2")
    budget = JSearchBudget(path=tmp_path / "jsearch_budget.json", monthly_cap=2)
    calls = []
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": {"status": "OK", "data": []}},
                                           calls)):
        job_sources.fetch_all([{"kind": "jsearch", "query": "q1"},
                               {"kind": "jsearch", "query": "q2"},
                               {"kind": "jsearch", "query": "q3"}],
                              jsearch_budget=budget)
    assert len(calls) == 2 and budget.used() == 2


def test_fetch_all_never_takes_jsearch_keys_from_a_source_entry(monkeypatch, capsys):
    monkeypatch.delenv("JSEARCH_API_KEYS", raising=False)
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=AssertionError("must not be called")):
        out = job_sources.fetch_all([{"kind": "jsearch", "query": "q",
                                      "api_keys": ["leaked"]}])
    assert out == []
    assert "JSEARCH_API_KEYS" in capsys.readouterr().out


def test_sources_yaml_registers_jsearch(monkeypatch, capsys):
    monkeypatch.setenv("JSEARCH_API_KEYS", "k1")
    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parent.parent / "sources.yaml").read_text())
    entries = [s for s in cfg["sources"] if s.get("kind") == "jsearch"]
    assert entries, "no jsearch entries in sources.yaml"
    for s in entries:
        assert s.get("country", "gb") in job_sources.JSEARCH_MARKETS
        # UK targeting has to be in the query TEXT: country=gb alone returns 0
        assert "UK" in s["query"]
    job_sources.reset_jsearch_key_rotation()
    with mock.patch("urllib.request.urlopen",
                    side_effect=_kv_router({"openwebninja.com": {"status": "OK", "data": []}})):
        out = job_sources.fetch_all(entries)
    printed = capsys.readouterr().out
    assert out == [] and "unknown source kind" not in printed


def test_env_example_documents_the_jsearch_variable_name():
    text = (pathlib.Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert "JSEARCH_API_KEYS=" in text
    for line in text.splitlines():
        if line.startswith("JSEARCH_API_KEYS="):
            assert line.split("=", 1)[1] == ""
