import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.enrich import company_domain, classify_headcount
from cv_tailor.job_sources import JobPosting


def _job(url, source="serpapi"):
    return JobPosting(source=source, org="Acme", title="AI Engineer", location="Remote - EU",
                      url=url, description="", raw_id="1")


def test_company_domain_skips_job_boards():
    assert company_domain(_job("https://acme.com/careers/ai-eng")) == "acme.com"
    assert company_domain(_job("https://boards.greenhouse.io/acme/jobs/1")) is None
    assert company_domain(_job("https://www.linkedin.com/jobs/view/123")) is None
    assert company_domain(_job("")) is None


def test_classify_headcount():
    assert classify_headcount("1-10") is True
    assert classify_headcount("201-500") is True
    assert classify_headcount("501-1000") is False
    assert classify_headcount("10001+") is False
    assert classify_headcount(None) is None
    assert classify_headcount("garbage") is None
