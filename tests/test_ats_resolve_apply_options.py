"""Strategy 0: the apply links SerpAPI already handed us. Cheaper and more
reliable than scraping a bot-walled Google SERP -- but it must keep the
org-match guard that stopped an EnthuZiastic job resolving to Cisco's Workday."""
from cv_tailor.ats_resolve import resolve_from_apply_options


def _entry(options, company="Acme", title="Engineer"):
    return {"company": company, "title": title, "apply_options": options}


def test_picks_adapter_host_matching_the_org():
    entry = _entry([
        {"label": "Apply on LinkedIn", "url": "https://www.linkedin.com/jobs/view/1"},
        {"label": "Apply on Ashby", "url": "https://jobs.ashbyhq.com/acme/abc-123"},
    ])
    assert resolve_from_apply_options(entry) == "https://jobs.ashbyhq.com/acme/abc-123"


def test_refuses_adapter_host_belonging_to_a_different_company():
    # The 2026-07 incident, generalized: an EnthuZiastic card whose only ATS
    # link was Cisco's. Wrong-company auto-apply is worse than needs_human.
    entry = _entry([{"label": "Apply", "url": "https://jobs.ashbyhq.com/cisco/xyz"}],
                   company="EnthuZiastic")
    assert resolve_from_apply_options(entry) is None


def test_org_match_may_come_from_the_path_not_just_the_subdomain():
    entry = _entry([{"label": "Apply", "url": "https://jobs.lever.co/acme/999"}])
    assert resolve_from_apply_options(entry) == "https://jobs.lever.co/acme/999"


def test_ignores_hosts_no_adapter_claims():
    entry = _entry([{"label": "Careers", "url": "https://acme.com/careers/1"}])
    assert resolve_from_apply_options(entry) is None


def test_strips_legal_suffix_before_matching():
    entry = _entry([{"label": "Apply", "url": "https://jobs.ashbyhq.com/acme/1"}],
                   company="Acme Inc")
    assert resolve_from_apply_options(entry) == "https://jobs.ashbyhq.com/acme/1"


def test_returns_none_without_company_or_options():
    assert resolve_from_apply_options({"company": "", "apply_options": []}) is None
    assert resolve_from_apply_options({"company": "Acme"}) is None


def test_ignores_parser_differential_host():
    entry = _entry([{"label": "Apply", "url": "https://evil.com\\.jobs.ashbyhq.com/acme/1"}])
    assert resolve_from_apply_options(entry) is None


def test_first_matching_option_wins():
    entry = _entry([
        {"label": "A", "url": "https://jobs.ashbyhq.com/acme/first"},
        {"label": "B", "url": "https://jobs.lever.co/acme/second"},
    ])
    assert resolve_from_apply_options(entry) == "https://jobs.ashbyhq.com/acme/first"


def test_resolve_ats_url_tries_apply_options_before_any_network(monkeypatch):
    from cv_tailor import ats_resolve

    def _boom(*a, **k):  # any network call here is a bug
        raise AssertionError("network strategy ran despite an apply_options hit")

    monkeypatch.setattr(ats_resolve, "_fetch_page", _boom)
    monkeypatch.setattr(ats_resolve, "resolve_from_boards", _boom)
    entry = {"company": "Acme", "title": "Engineer",
             "apply_target": "https://www.google.com/search?ibp=htl;jobs",
             "apply_options": [{"label": "Apply", "url": "https://jobs.ashbyhq.com/acme/1"}]}
    assert ats_resolve.resolve_ats_url(entry) == "https://jobs.ashbyhq.com/acme/1"
