import io, json, sys, pathlib
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources
from cv_tailor.budget import SerpBudget
from cv_tailor.job_sources import _best_company_url


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
    # a real key must be present -- fetch_serpapi now checks the key BEFORE
    # consulting the budget, so a missing key would short-circuit before
    # budget.take() is ever reached (see the two key-check-order tests below)
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)), \
         mock.patch.dict("os.environ", {"SERPAPI_API_KEY": "k"}, clear=True):
        job_sources.fetch_all(sources, serp_budget=budget)
    # only the first query consumed the shared cap of 1; the second was blocked
    assert budget.used() == 1


def test_fetch_serpapi_checks_api_key_before_touching_budget(tmp_path):
    """A missing/unset key must never consume budget -- the key check runs
    BEFORE budget.take() so a misconfigured SERPAPI_API_KEY doesn't silently
    burn the shared monthly cap with zero real queries (same failure class as
    the repo's Azure-key twice-bitten history)."""
    budget = SerpBudget(path=tmp_path / "serpapi_budget.json", monthly_cap=5)
    with mock.patch("urllib.request.urlopen") as urlopen, \
         mock.patch.dict("os.environ", {}, clear=True):
        out = job_sources.fetch_serpapi("AI engineer", budget=budget)
    urlopen.assert_not_called()
    assert out == []
    assert budget.used() == 0


def test_fetch_serpapi_key_check_order_preserved_when_key_present(tmp_path):
    # sanity check the other direction: a present key still reaches (and
    # consumes) the budget gate as before
    budget = SerpBudget(path=tmp_path / "serpapi_budget.json", monthly_cap=5)
    payload = {"jobs_results": []}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        job_sources.fetch_serpapi("AI engineer", api_key="k", budget=budget)
    assert budget.used() == 1


# --- Fix A: _best_company_url apply-link coherence (real incident: a SerpAPI
# "EnthuZiastic - Generative AI Automation Engineer - Remote" card whose only
# non-board apply_options link actually pointed at Cisco's Workday page --
# a hybrid, US-onsite role at a different company entirely) ---

def test_best_company_url_real_incident_cross_listed_link_falls_back_to_share_link():
    apply_options = [{
        "title": "Cisco",
        "link": "https://cisco.wd5.myworkdayjobs.com/en-US/Cisco_Careers/job/"
                "Automation-AI-Ops-Engineer",
    }]
    share_link = "https://www.google.com/search?ibp=htl;jobs#htivrt=jobs&htidocid=abc123"
    url = _best_company_url("EnthuZiastic", apply_options, share_link)
    # Must NEVER pick a link that plainly names a different company.
    assert url == share_link


def test_best_company_url_prefers_matching_company_domain():
    apply_options = [
        {"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/1"},
        {"title": "Careers", "link": "https://careers.acme.com/jobs/1"},
    ]
    url = _best_company_url("Acme Inc", apply_options, "https://share/x")
    assert url == "https://careers.acme.com/jobs/1"


def test_best_company_url_matches_ats_hosted_path():
    apply_options = [{"title": "Lever", "link": "https://jobs.lever.co/acme/some-id"}]
    url = _best_company_url("Acme Inc", apply_options, "https://share/x")
    assert url == "https://jobs.lever.co/acme/some-id"


def test_best_company_url_board_only_falls_back_to_share_link():
    apply_options = [
        {"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/1"},
        {"title": "Indeed", "link": "https://www.indeed.com/jobs/1"},
    ]
    url = _best_company_url("Acme Inc", apply_options, "https://share/x")
    assert url == "https://share/x"


def test_best_company_url_no_apply_options_falls_back_to_share_link():
    url = _best_company_url("Acme Inc", [], "https://share/x")
    assert url == "https://share/x"
    url2 = _best_company_url("Acme Inc", None, "https://share/x")
    assert url2 == "https://share/x"


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
