# tests/test_enrich.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.enrich import is_smb, smb_hint
from cv_tailor.job_sources import JobPosting


def _job(source):
    return JobPosting(source=source, org="Acme", title="AI Engineer",
                      location="Remote - EU", url="https://x", description="", raw_id="1")


def test_startup_ats_is_smb():
    assert is_smb(_job("ashby")) is True
    assert is_smb(_job("greenhouse")) is True
    assert is_smb(_job("lever")) is True


def test_enterprise_hris_not_smb():
    assert is_smb(_job("workday")) is False
    assert is_smb(_job("successfactors")) is False


def test_smb_hint_string():
    assert "startup" in smb_hint(_job("greenhouse")).lower()
