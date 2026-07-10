import io, json, sys, pathlib
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources
from cv_tailor.budget import SerpBudget


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


def test_fetch_serpapi_consults_budget_and_skips_network_when_exhausted(tmp_path, capsys):
    budget = SerpBudget(path=tmp_path / "serpapi_budget.json", monthly_cap=0)
    with mock.patch("urllib.request.urlopen") as urlopen:
        out = job_sources.fetch_serpapi("AI engineer", api_key="k", budget=budget)
    urlopen.assert_not_called()
    assert out == []
    printed = capsys.readouterr().out
    assert "serpapi budget exhausted" in printed
    assert "cap 0/mo" in printed
    assert "AI engineer" in printed


def test_fetch_serpapi_takes_from_budget_when_available(tmp_path):
    budget = SerpBudget(path=tmp_path / "serpapi_budget.json", monthly_cap=5)
    payload = {"jobs_results": []}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        job_sources.fetch_serpapi("AI engineer", api_key="k", budget=budget)
    assert budget.used() == 1


def test_fetch_all_threads_one_shared_budget_across_serpapi_sources(tmp_path):
    budget = SerpBudget(path=tmp_path / "serpapi_budget.json", monthly_cap=1)
    payload = {"jobs_results": []}
    sources = [
        {"kind": "serpapi", "query": "AI engineer remote europe"},
        {"kind": "serpapi", "query": "content producer remote part-time"},
    ]
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        job_sources.fetch_all(sources, serp_budget=budget)
    # only the first query consumed the shared cap of 1; the second was blocked
    assert budget.used() == 1


def test_fetch_all_without_budget_is_unbudgeted(capsys):
    # serp_budget=None (default) preserves pre-budget behavior: fetch_serpapi
    # runs its normal (unbudgeted) logic -- reaching the api_key check below
    # the budget gate, not the "budget exhausted" short-circuit above it.
    with mock.patch("urllib.request.urlopen") as urlopen, \
         mock.patch.dict("os.environ", {}, clear=True):
        job_sources.fetch_all([{"kind": "serpapi", "query": "AI engineer"}])
    urlopen.assert_not_called()
    printed = capsys.readouterr().out
    assert "SERPAPI_API_KEY not set" in printed
    assert "budget exhausted" not in printed
