# tests/test_gates.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.gates import (
    is_remote, is_eu_eligible, has_target_keyword, passes_gate1,
    matched_tracks, passes_gate1_tracks,
)
from cv_tailor.job_sources import JobPosting

KW = ["ai engineer", "python", "agentic", "automation"]

TRACKS = {
    "ai": {"keywords": ["ai engineer", "python", "agentic", "automation"]},
    "content": {"keywords": ["content producer", "video editor", "copywriter"]},
}


def _job(title, location, desc=""):
    return JobPosting(source="greenhouse", org="Acme", title=title, location=location,
                      url="https://x", description=desc, raw_id="1")


def test_remote_detection():
    assert is_remote("Remote - EMEA", "") is True
    assert is_remote("Berlin, Germany", "Fully remote within Europe") is True
    assert is_remote("New York (On-site)", "Onsite role") is False


def test_eu_eligibility():
    assert is_eu_eligible("Remote - Europe", "") is True
    assert is_eu_eligible("Remote - Global", "Work from anywhere") is True
    assert is_eu_eligible("Remote - US only", "Must be US-based") is False


def test_keyword_presence():
    assert has_target_keyword("Senior AI Engineer, Python", KW) is True
    assert has_target_keyword("Sales Development Rep", KW) is False


def test_passes_gate1_truth_table():
    good = _job("AI Engineer", "Remote - Europe", "Python, agentic systems")
    assert passes_gate1(good, KW) is True
    not_remote = _job("AI Engineer", "Berlin (On-site)", "Python")
    assert passes_gate1(not_remote, KW) is False
    wrong_geo = _job("AI Engineer", "Remote - US only", "Python")
    assert passes_gate1(wrong_geo, KW) is False
    wrong_role = _job("Account Executive", "Remote - Europe", "quota carrying")
    assert passes_gate1(wrong_role, KW) is False


def test_matched_tracks_order_and_membership():
    ai_job = _job("AI Engineer", "Remote - Europe", "Python, agentic systems")
    assert matched_tracks(ai_job, TRACKS) == ["ai"]

    content_job = _job("Content Producer", "Remote - Europe", "video editor role")
    assert matched_tracks(content_job, TRACKS) == ["content"]

    both_job = _job("AI Content Producer", "Remote - Europe", "python and video editor")
    assert matched_tracks(both_job, TRACKS) == ["ai", "content"]  # config order

    neither_job = _job("Sales Rep", "Remote - Europe", "quota carrying")
    assert matched_tracks(neither_job, TRACKS) == []


def test_passes_gate1_tracks_winner_and_ties():
    ai_job = _job("AI Engineer", "Remote - Europe", "Python, agentic systems")
    assert passes_gate1_tracks(ai_job, TRACKS) == "ai"

    content_job = _job("Content Producer", "Remote - Europe", "video editor role")
    assert passes_gate1_tracks(content_job, TRACKS) == "content"

    both_job = _job("AI Content Producer", "Remote - Europe", "python and video editor")
    assert passes_gate1_tracks(both_job, TRACKS) == "ai"  # tie -> ai wins (config order)

    neither_job = _job("Sales Rep", "Remote - Europe", "quota carrying")
    assert passes_gate1_tracks(neither_job, TRACKS) is None


def test_passes_gate1_tracks_geo_gates_still_apply():
    not_remote = _job("AI Engineer", "Berlin (On-site)", "Python")
    assert passes_gate1_tracks(not_remote, TRACKS) is None
    wrong_geo = _job("AI Engineer", "Remote - US only", "Python")
    assert passes_gate1_tracks(wrong_geo, TRACKS) is None
