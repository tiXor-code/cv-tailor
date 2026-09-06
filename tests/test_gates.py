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


# --- Fix C: cheap Gate 1 hybrid-without-EU-signal drop (real incident:
# EnthuZiastic/Cisco cross-listing -- a "Remote" job whose actual JD anchors
# the role to onsite US-city cadence) ---

def test_passes_gate1_drops_hybrid_without_eu_signal():
    # Mirrors the real Cisco JD behind the mislinked EnthuZiastic apply
    # option: a "remote-friendly"/"global" blurb sits next to a hard
    # onsite-days-per-week requirement in named US cities, no EU signal.
    cisco_like = _job(
        "Automation AI Ops Engineer",
        "Raleigh, North Carolina or San Jose, California",
        "We're a global, remote-friendly company. Onsite 3 days per week in "
        "Raleigh, North Carolina or San Jose, California is required for this role.",
    )
    assert passes_gate1(cisco_like, KW) is False
    assert passes_gate1_tracks(cisco_like, TRACKS) is None


def test_passes_gate1_keeps_hybrid_with_eu_signal():
    # A genuinely hybrid EU role (Bucharest office) must NOT be dropped --
    # the check is recall-favoring and only fires with zero EU signal.
    bucharest_hybrid = _job(
        "AI Engineer",
        "Bucharest, Romania (Remote/Hybrid)",
        "Python, agentic systems. Remote-first with hybrid flexibility -- "
        "2 days a week in our Bucharest office for those nearby.",
    )
    assert passes_gate1(bucharest_hybrid, KW) is True
    assert passes_gate1_tracks(bucharest_hybrid, TRACKS) == "ai"


def test_passes_gate1_plain_remote_eu_still_passes():
    plain_remote_eu = _job("AI Engineer", "Remote - Europe", "Python, agentic systems")
    assert passes_gate1(plain_remote_eu, KW) is True
    assert passes_gate1_tracks(plain_remote_eu, TRACKS) == "ai"


def test_norina_jobs_compat_signatures_unchanged():
    """norina-jobs imports is_remote/is_eu_eligible/has_target_keyword directly
    (src/norina/gate.py) and calls them positionally as (location, description)
    / (text, keywords). Fix C must not change these signatures."""
    import inspect
    assert list(inspect.signature(is_remote).parameters) == ["location", "description"]
    assert list(inspect.signature(is_eu_eligible).parameters) == ["location", "description"]
    assert list(inspect.signature(has_target_keyword).parameters) == ["text", "keywords"]
    assert list(inspect.signature(passes_gate1).parameters) == ["job", "keywords"]
