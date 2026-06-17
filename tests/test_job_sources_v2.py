# tests/test_job_sources_v2.py
import io, json, sys, pathlib
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources


def _fake_urlopen(payload):
    buf = io.BytesIO(json.dumps(payload).encode())
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    return cm


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
