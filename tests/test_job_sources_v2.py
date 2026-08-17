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
