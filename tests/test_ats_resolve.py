"""Aggregator-to-ATS resolver. All URLs/values are dummies (canary rule).

The safety property: the resolver may only ever return a URL that (a) an
adapter claims AND (b) belongs to THIS company -- anything ambiguous or
untrusted resolves to None and the job stays needs_human."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cv_tailor import ats_resolve


PAGE = """
<html><body>
<a href="https://remoteok.com/some-internal-nav">nav</a>
<a href="https://job-boards.greenhouse.io/othercorp/jobs/999">wrong company ATS</a>
<a href="https://jobs.ashbyhq.com/acmelabs/11111111-2222">Apply at Acme Labs</a>
<a href="//jobs.lever.co/acmelabs/abc#apply">protocol-relative</a>
</body></html>
"""


def test_resolve_from_page_picks_own_company_adapter_link():
    hit = ats_resolve.resolve_from_page(PAGE, "Acme Labs")
    assert hit == "https://jobs.ashbyhq.com/acmelabs/11111111-2222"


def test_resolve_from_page_never_returns_another_companys_ats():
    # OtherCorp's greenhouse link is adapter-claimed but slug-mismatched
    hit = ats_resolve.resolve_from_page(
        PAGE.replace('jobs.ashbyhq.com/acmelabs/11111111-2222', 'x.example/nope'),
        "Acme Labs")
    # only the lever protocol-relative link remains for acmelabs
    assert hit == "https://jobs.lever.co/acmelabs/abc"


def test_resolve_from_page_rejects_parser_differential_urls():
    evil = '<a href="https://evil.example\\.jobs.ashbyhq.com/acmelabs/1">x</a>'
    assert ats_resolve.resolve_from_page(evil, "Acme Labs") is None


def test_resolve_from_page_requires_company():
    assert ats_resolve.resolve_from_page(PAGE, "") is None


def test_title_match_rules():
    assert ats_resolve._title_match("Forward Deployed Engineer",
                                    "Forward Deployed Engineer, Agentic Platform")
    assert ats_resolve._title_match("Senior Backend Engineer (Remote - EMEA)",
                                    "Senior Backend Engineer")
    assert ats_resolve._title_match("Engineer", "Engineer")  # exact equality always ok
    # containment needs the short side >= 10 normalized chars ("engineer" is 8)
    assert not ats_resolve._title_match("Engineer", "Engineer II")
    assert not ats_resolve._title_match("Designer", "Senior Staff Designer of Things")


def _job(title, url):
    return SimpleNamespace(title=title, url=url)


def test_resolve_from_boards_unique_match(monkeypatch):
    monkeypatch.setattr(ats_resolve, "fetch_ashby_org", lambda slug, name: [
        _job("Forward Deployed Engineer, Agentic Platform",
             "https://jobs.ashbyhq.com/acmelabs/aaa")] if slug == "acmelabs" else [])
    monkeypatch.setattr(ats_resolve, "fetch_greenhouse_org", lambda s, n: [])
    monkeypatch.setattr(ats_resolve, "fetch_lever_org", lambda s, n: [])
    hit = ats_resolve.resolve_from_boards("Acme Labs", "Forward Deployed Engineer")
    assert hit == "https://jobs.ashbyhq.com/acmelabs/aaa"


def test_resolve_from_boards_ambiguity_refuses(monkeypatch):
    jobs = [_job("Backend Engineer, Payments", "https://jobs.ashbyhq.com/a/1"),
            _job("Backend Engineer, Infra", "https://jobs.ashbyhq.com/a/2")]
    monkeypatch.setattr(ats_resolve, "fetch_ashby_org", lambda s, n: jobs)
    monkeypatch.setattr(ats_resolve, "fetch_greenhouse_org", lambda s, n: [])
    monkeypatch.setattr(ats_resolve, "fetch_lever_org", lambda s, n: [])
    assert ats_resolve.resolve_from_boards("Acme", "Backend Engineer") is None


def test_resolve_from_boards_exact_beats_ambiguous(monkeypatch):
    jobs = [_job("Backend Engineer", "https://jobs.ashbyhq.com/a/1"),
            _job("Backend Engineer, Infra", "https://jobs.ashbyhq.com/a/2")]
    monkeypatch.setattr(ats_resolve, "fetch_ashby_org", lambda s, n: jobs)
    monkeypatch.setattr(ats_resolve, "fetch_greenhouse_org", lambda s, n: [])
    monkeypatch.setattr(ats_resolve, "fetch_lever_org", lambda s, n: [])
    assert ats_resolve.resolve_from_boards("Acme", "Backend Engineer") == \
        "https://jobs.ashbyhq.com/a/1"


def test_resolve_from_boards_rejects_unclaimed_host(monkeypatch):
    monkeypatch.setattr(ats_resolve, "fetch_ashby_org", lambda s, n: [
        _job("Backend Engineer Role X", "https://careers.acme.example/1")])
    monkeypatch.setattr(ats_resolve, "fetch_greenhouse_org", lambda s, n: [])
    monkeypatch.setattr(ats_resolve, "fetch_lever_org", lambda s, n: [])
    assert ats_resolve.resolve_from_boards("Acme", "Backend Engineer Role X") is None


def test_resolve_ats_url_page_first_then_boards(monkeypatch):
    entry = {"company": "Acme Labs", "title": "Forward Deployed Engineer",
             "apply_target": "https://remoteok.example/listing/1"}
    monkeypatch.setattr(ats_resolve, "_fetch_page", lambda url: PAGE)
    hit = ats_resolve.resolve_ats_url(entry)
    assert hit == "https://jobs.ashbyhq.com/acmelabs/11111111-2222"

    # page yields nothing -> falls through to board probe
    monkeypatch.setattr(ats_resolve, "_fetch_page", lambda url: "")
    monkeypatch.setattr(ats_resolve, "fetch_ashby_org", lambda s, n: [
        _job("Forward Deployed Engineer", "https://jobs.ashbyhq.com/acmelabs/bbb")])
    monkeypatch.setattr(ats_resolve, "fetch_greenhouse_org", lambda s, n: [])
    monkeypatch.setattr(ats_resolve, "fetch_lever_org", lambda s, n: [])
    assert ats_resolve.resolve_ats_url(entry) == "https://jobs.ashbyhq.com/acmelabs/bbb"


def test_resolve_ats_url_requires_company_and_title():
    assert ats_resolve.resolve_ats_url({"company": "", "title": "X"}) is None
    assert ats_resolve.resolve_ats_url({"company": "X", "title": ""}) is None


def test_fetch_page_degrades_to_empty_on_failure():
    assert ats_resolve._fetch_page("http://127.0.0.1:1/nope") == ""


def test_region_preference_breaks_exact_title_ties(monkeypatch):
    """Two postings share the exact title (US vs Middle East) and a third is
    the (UK/Europe) variant -- the European one is the only correct target."""
    jobs = [
        _job("Forward Deployed Engineer, Agentic Platform",
             "https://jobs.ashbyhq.com/a/us"),
        _job("Forward Deployed Engineer, Agentic Platform",
             "https://jobs.ashbyhq.com/a/me"),
        _job("Forward Deployed Engineer, Agentic Platform (UK/Europe)",
             "https://jobs.ashbyhq.com/a/eu"),
    ]
    jobs[0].location = "United States - Canada"
    jobs[1].location = "Middle East - Dubai"
    jobs[2].location = "Remote - Europe"
    monkeypatch.setattr(ats_resolve, "fetch_ashby_org", lambda s, n: jobs)
    monkeypatch.setattr(ats_resolve, "fetch_greenhouse_org", lambda s, n: [])
    monkeypatch.setattr(ats_resolve, "fetch_lever_org", lambda s, n: [])
    hit = ats_resolve.resolve_from_boards("Acme", "Forward Deployed Engineer Agentic Platform")
    assert hit == "https://jobs.ashbyhq.com/a/eu"


def test_region_preference_still_refuses_when_no_european_posting(monkeypatch):
    jobs = [_job("Backend Engineer, Payments Team", "https://jobs.ashbyhq.com/a/1"),
            _job("Backend Engineer, Payments Team", "https://jobs.ashbyhq.com/a/2")]
    jobs[0].location = "US"
    jobs[1].location = "Singapore"
    monkeypatch.setattr(ats_resolve, "fetch_ashby_org", lambda s, n: jobs)
    monkeypatch.setattr(ats_resolve, "fetch_greenhouse_org", lambda s, n: [])
    monkeypatch.setattr(ats_resolve, "fetch_lever_org", lambda s, n: [])
    assert ats_resolve.resolve_from_boards("Acme", "Backend Engineer, Payments Team") is None
