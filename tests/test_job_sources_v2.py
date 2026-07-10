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
