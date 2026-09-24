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


# --- Calibration, Teodor 2026-09-24: remote only, ANY country that will hire
# him (US, EU, Asia, Cyprus), no German/French. Scout's own gate
# (passes_gate1_tracks); norina-jobs' imports above keep their behaviour. ---

def test_a_worldwide_remote_job_from_a_us_company_now_passes():
    job = _job("AI Automation Engineer", "Remote (Worldwide)",
               "US-based startup. Build agentic automations in Python. Contractors welcome.")
    assert passes_gate1_tracks(job, TRACKS) == "ai"


def test_remote_restricted_to_a_non_eu_country_is_dropped():
    """Measured 2026-09-24: YC postings located "Remote (US)" / "Remote (CA)"
    scored 9-10 -- the scorer does not cap them -- so the gate does. Only
    when the location names no EU country, Europe or worldwide."""
    for loc in ("Remote - United States", "Remote (US)", "San Francisco, CA, US / Remote (US)",
                "CA / Remote (CA)", "Remote (US; CA)", "Remote, US", "Remote - Canada"):
        assert passes_gate1_tracks(_job("AI Engineer", loc, "Agentic AI, Python."), TRACKS) is None, loc


def test_remote_listing_several_countries_including_the_eu_passes():
    for loc in ("Remote, Canada; Remote, Poland", "Remote (US or Europe)", "Remote - Worldwide",
                "Berlin, BE, DE / Remote", "Remote"):
        assert passes_gate1_tracks(_job("AI Engineer", loc, "Agentic AI, Python."), TRACKS) == "ai", loc


def test_residency_authorization_or_clearance_requirements_are_dropped():
    for desc in (
        "Agentic AI. You must be located in the United States.",
        "Python automation. Must be authorized to work in the US without sponsorship.",
        "AI engineer role. Active security clearance required.",
        "Agentic workflows. Candidates must reside in Canada.",
    ):
        assert passes_gate1_tracks(_job("AI Engineer", "Remote", desc), TRACKS) is None, desc


def test_an_eu_residency_requirement_is_fine_he_is_an_eu_citizen():
    job = _job("AI Engineer", "Remote", "Agentic AI. Must be based in the EU.")
    assert passes_gate1_tracks(job, TRACKS) == "ai"


def test_an_onsite_or_hybrid_job_is_dropped_even_in_europe():
    for loc in ("Bucharest, Romania (Hybrid)", "Berlin (On-site)", "Lisbon - In office"):
        assert passes_gate1_tracks(_job("AI Engineer", loc, "Python agentic"), TRACKS) is None, loc


def test_required_german_or_french_is_dropped():
    for desc in (
        "Agentic AI in Python. Fluent German is required.",
        "LLM automation. You speak French at C1 level or above.",
        "AI engineer. Business-level German (mandatory).",
    ):
        assert passes_gate1_tracks(_job("AI Engineer", "Remote - Europe", desc), TRACKS) is None, desc
    assert passes_gate1_tracks(
        _job("Senior AI Engineer (German Speaking)", "Remote - Europe", "Agentic AI in Python."),
        TRACKS) is None


def test_german_or_french_as_a_plus_is_kept():
    for desc in (
        "Agentic AI in Python. German is a plus.",
        "LLM automation. French would be nice to have.",
        "Remote across Europe including Germany and France. Python agentic.",
        "Strong engineering culture in Germany. Agentic AI in Python.",
        "Excellent benefits for our French and German offices. Agentic AI.",
    ):
        assert passes_gate1_tracks(_job("AI Engineer", "Remote - Europe", desc), TRACKS) == "ai", desc


def test_a_posting_written_in_german_or_french_is_dropped():
    de = ("Wir suchen einen AI Engineer (m/w/d) für unser Team. Du entwickelst agentic "
          "Workflows mit Python und bringst Erfahrung mit LLMs mit. Wir bieten dir die "
          "Möglichkeit, remote zu arbeiten, und ein tolles Team.")
    fr = ("Nous recherchons un AI Engineer pour notre équipe. Vous développerez des "
          "workflows agentic avec Python et vous avez une expérience des LLMs. Nous "
          "proposons un poste en remote et une équipe formidable.")
    assert passes_gate1_tracks(_job("AI Engineer", "Remote", de), TRACKS) is None
    assert passes_gate1_tracks(_job("AI Engineer", "Remote", fr), TRACKS) is None


def test_requires_excluded_language_is_the_attributable_reason():
    from cv_tailor.gates import requires_excluded_language
    assert requires_excluded_language("AI Engineer", "Fluent German is required.")
    assert not requires_excluded_language("AI Engineer", "German is a plus.")


# --- Calibration round 2, from Teodor's 40 rated postings (2026-09-24) -----
# He marked Dutch-language postings "not English friendly", a Greek
# requirement a no, and "hybrid, hard pass" -- including hybrid mentioned only
# in a perks list under a LinkedIn "Remote" label.

_DUTCH = ("Vanuit Utrecht wordt op dit moment een nieuwe Nederlandse Data & AI-practice "
          "opgebouwd. Je stapt in op een moment waarop het team groeit en je combineert "
          "daarmee de ondernemingsruimte van een startup met de zekerheid van een groot "
          "bedrijf. Wij bieden een dienstverband voor 32 tot 40 uur en werken met LLM agents.")
_ROMANIAN = ("Căutăm un AI Engineer care să construiască agenți și automatizări cu LLM pentru "
             "clienții noștri. Vei lucra remote, cu o echipă mică, și vei livra rapid soluții "
             "de automatizare. Este un rol pentru cineva care vrea să construiască produse.")


def test_a_posting_written_in_any_language_but_english_or_romanian_is_dropped():
    assert passes_gate1_tracks(_job("AI Engineer", "Remote", _DUTCH), TRACKS) is None


def test_a_romanian_posting_is_kept_he_speaks_romanian():
    assert passes_gate1_tracks(_job("AI Engineer", "Remote", _ROMANIAN + " agentic"), TRACKS) == "ai"


def test_any_required_non_english_language_is_dropped():
    for desc in (
        "Agentic AI. Excellent communication skills in Greek and English.",
        "LLM automation. Fluent Dutch is required.",
        "AI engineer. Native Spanish speaker.",
    ):
        assert passes_gate1_tracks(_job("AI Engineer", "Remote - Europe", desc), TRACKS) is None, desc


def test_another_language_as_a_merit_is_kept():
    desc = ("Agentic AI in Python. Meriting: prior consulting experience, "
            "Swedish language skills are a plus.")
    assert passes_gate1_tracks(_job("AI Engineer", "Remote - Europe", desc), TRACKS) == "ai"


def test_hybrid_work_in_the_perks_under_a_remote_label_is_dropped():
    for desc in (
        "Agentic AI in Python. The perks: 30 days vacation, hybrid work and flexible hours.",
        "LLM automation. Your work-life balance supported by a hybrid working model.",
    ):
        assert passes_gate1_tracks(_job("AI Engineer", "Stockholm (Remote)", desc), TRACKS) is None, desc


def test_hybrid_as_a_technical_term_is_not_hybrid_work():
    desc = "Agentic AI in Python. RAG with vector search, hybrid retrieval and reranking. Fully remote."
    assert passes_gate1_tracks(_job("AI Engineer", "Remote - Europe", desc), TRACKS) == "ai"


def test_a_capped_time_abroad_perk_means_residency_is_required():
    """Wealthsimple ('Remote - Anywhere'): '90 days away: work outside Canada for
    up to 90 days per year' -- you must live in Canada. He marked it No."""
    desc = "Agentic AI. 90 days away: work outside Canada for up to 90 days per year."
    assert passes_gate1_tracks(_job("AI Engineer", "Remote - Anywhere", desc), TRACKS) is None
