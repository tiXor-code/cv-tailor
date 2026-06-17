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


def test_is_smb_uses_hunter_for_serpapi(tmp_path, monkeypatch):
    import cv_tailor.enrich as enrich
    from cv_tailor.cache import connect, get_enrichment
    conn = connect(tmp_path / "jobs.db")
    calls = {"n": 0}
    def fake_hunter(domain, api_key=None):
        calls["n"] += 1
        return "11-50" if domain == "acme.com" else "5001-10000"
    monkeypatch.setattr(enrich, "hunter_headcount", fake_hunter)

    smb_job = _job("https://acme.com/careers/1")
    big_job = _job("https://megacorp.com/jobs/1")
    assert enrich.is_smb(smb_job, conn) is True
    assert enrich.is_smb(big_job, conn) is False
    # verdict cached -> second call doesn't re-hit hunter
    assert enrich.is_smb(smb_job, conn) is True
    assert calls["n"] == 2  # one per distinct domain only


def test_is_smb_provenance_unchanged_without_conn():
    assert _job("x", source="greenhouse") and __import__("cv_tailor.enrich", fromlist=["is_smb"]).is_smb(_job("x", source="greenhouse")) is True
    from cv_tailor.enrich import is_smb
    assert is_smb(_job("x", source="workday")) is False
