"""apply_options survives the scan so the resolver and the human both see
every apply link SerpAPI returned -- not just the one _best_company_url picks."""
from cv_tailor.job_sources import JobPosting, _apply_option_links


def test_normalizes_title_and_link_pairs():
    raw = [
        {"title": "Apply on EnthuZiastic", "link": "https://enthuziastic.com/careers/42"},
        {"title": "Apply on LinkedIn", "link": "https://www.linkedin.com/jobs/view/99"},
    ]
    assert _apply_option_links(raw) == [
        {"label": "Apply on EnthuZiastic", "url": "https://enthuziastic.com/careers/42"},
        {"label": "Apply on LinkedIn", "url": "https://www.linkedin.com/jobs/view/99"},
    ]


def test_drops_non_http_schemes():
    raw = [
        {"title": "x", "link": "javascript:alert(1)"},
        {"title": "y", "link": "data:text/html,hi"},
        {"title": "z", "link": "ftp://example.com/f"},
        {"title": "ok", "link": "https://example.com/job"},
    ]
    assert _apply_option_links(raw) == [{"label": "ok", "url": "https://example.com/job"}]


def test_drops_parser_differential_hosts():
    # safe_hostname returns "" for these; they must never reach an <a href>
    # or a host-allowlist decision (1d9c700).
    raw = [
        {"title": "bad", "link": "https://evil.com\\.jobs.ashbyhq.com/acme/1"},
        {"title": "good", "link": "https://jobs.ashbyhq.com/acme/1"},
    ]
    assert _apply_option_links(raw) == [
        {"label": "good", "url": "https://jobs.ashbyhq.com/acme/1"}
    ]


def test_dedupes_by_url_keeping_first_label():
    raw = [
        {"title": "First", "link": "https://example.com/job"},
        {"title": "Second", "link": "https://example.com/job"},
    ]
    assert _apply_option_links(raw) == [{"label": "First", "url": "https://example.com/job"}]


def test_caps_at_eight_options():
    raw = [{"title": f"o{i}", "link": f"https://example.com/{i}"} for i in range(20)]
    assert len(_apply_option_links(raw)) == 8


def test_missing_or_blank_title_falls_back_to_hostname():
    raw = [{"link": "https://jobs.lever.co/acme/1"}, {"title": "  ", "link": "https://x.io/2"}]
    assert _apply_option_links(raw) == [
        {"label": "jobs.lever.co", "url": "https://jobs.lever.co/acme/1"},
        {"label": "x.io", "url": "https://x.io/2"},
    ]


def test_handles_none_and_junk_input():
    assert _apply_option_links(None) == []
    assert _apply_option_links([]) == []
    assert _apply_option_links(["not-a-dict", None, {}]) == []


def test_job_posting_defaults_to_empty_list():
    job = JobPosting(source="ashby", org="Acme", title="Eng", location="EU",
                     url="https://jobs.ashbyhq.com/acme/1", description="", raw_id="1")
    assert job.apply_options == []


def test_job_posting_default_is_not_shared_between_instances():
    a = JobPosting(source="s", org="o", title="t", location="l", url="u", description="d", raw_id="r")
    b = JobPosting(source="s", org="o", title="t", location="l", url="u", description="d", raw_id="r")
    a.apply_options.append({"label": "x", "url": "https://example.com"})
    assert b.apply_options == []
