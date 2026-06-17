import io, json, sys, pathlib
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources


def _fake_urlopen(payload):
    buf = io.BytesIO(json.dumps(payload).encode())
    cm = mock.MagicMock(); cm.__enter__.return_value = buf
    return cm


def test_serpapi_maps_fields():
    payload = {"jobs_results": [{
        "title": "AI Engineer", "company_name": "Acme", "location": "Remote - Europe",
        "via": "via LinkedIn", "share_link": "https://serpapi.com/x", "description": "Python agents",
        "job_id": "ID123",
        "detected_extensions": {"work_from_home": True},
        "apply_options": [{"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/1"},
                          {"title": "Acme", "link": "https://acme.com/careers/1"}]}]}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_serpapi("AI engineer", location="Europe", api_key="k")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "serpapi" and j.org == "Acme" and j.title == "AI Engineer"
    assert j.raw_id == "ID123"
    # url prefers the non-job-board apply link (company careers page) for Hunter domain extraction
    assert j.url == "https://acme.com/careers/1"


def test_fetch_all_dispatches_serpapi():
    payload = {"jobs_results": []}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        out = job_sources.fetch_all([{"kind": "serpapi", "query": "AI engineer", "location": "Europe"}])
    assert out == []
