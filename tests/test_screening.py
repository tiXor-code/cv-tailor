from types import SimpleNamespace

import pytest

from cv_tailor.screening import Answer, Question, answer_question, build_llm_messages


_PROFILE = {
    "contact": {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+44 20 7946 0958",
        "location": "London, UK",
        "website": "ada.example.com",
        "linkedin": "linkedin.com/in/ada",
        "github": "github.com/ada",
    },
}

# Same profile, but missing the "website" field -- profile.yaml fixtures in
# this repo don't always carry one.
_PROFILE_NO_WEBSITE = {
    "contact": {k: v for k, v in _PROFILE["contact"].items() if k != "website"},
}

_ANSWERS = {
    "salary_fulltime_gross_eur_month": 4500,
    "salary_fulltime_net_eur_month": "2600-2900",
    "hourly_rate_min_eur": 30,
    "availability_parttime": "Tuesday and Thursday evenings",
    "work_authorization": "EU citizen, can work anywhere in the EU.",
    "notice_period": "30 calendar days",
    "relocation": "Not open to relocation; remote only.",
    "links": {
        "website": "https://example.com",
        "linkedin": "https://linkedin.com/in/example",
        "github": "https://github.com/example",
    },
}


def _q(label, kind="text", required=True, options=()):
    return Question(label=label, kind=kind, required=required, options=tuple(options))


class _FakeClient:
    """Returns queued responses; records how many times it was called."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        text = self._replies.pop(0)
        msg = SimpleNamespace(content=text)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


# ---------------------------------------------------------------------------
# Deterministic tier: answers.yaml keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,key",
    [
        ("Are you open to relocation for this role?", "relocation"),
        ("What is your minimum hourly rate?", "hourly_rate_min_eur"),
        ("What is your notice period?", "notice_period"),
        ("Are you legally authorized to work in the EU?", "work_authorization"),
        ("Do you require visa sponsorship?", "work_authorization"),
        ("What is your availability for part-time work?", "availability_parttime"),
    ],
)
def test_deterministic_answers_yaml_keys(label, key):
    q = _q(label)
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer(str(_ANSWERS[key]), f"answers:{key}")


def test_deterministic_salary_gross_by_default():
    # CHANGED (fix 4): a text salary field now STATES the currency instead of
    # typing a bare number a US employer could read as USD.
    q = _q("What is your expected monthly salary?")
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer(
        f"{_ANSWERS['salary_fulltime_gross_eur_month']} EUR gross per month",
        "answers:salary_fulltime_gross_eur_month",
    )


def test_deterministic_salary_net_when_label_says_net():
    # CHANGED (fix 4): net salary text field states the currency too.
    q = _q("What is your expected net monthly salary?")
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer(
        f"{_ANSWERS['salary_fulltime_net_eur_month']} EUR net per month",
        "answers:salary_fulltime_net_eur_month",
    )


def test_relocation_not_confused_with_generic_location():
    # "relocation" contains "location" as a substring -- must not fall into
    # the generic contact.location handler.
    q = _q("Are you open to relocation to Berlin?")
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out.grounded_in == "answers:relocation"


def test_deterministic_missing_answers_key_falls_through():
    q = _q("What is your notice period?")
    out = answer_question(q, _PROFILE, {**_ANSWERS, "notice_period": ""})
    assert out is None  # matched category, but no grounded value -- no client to fall back on


# ---------------------------------------------------------------------------
# Deterministic tier: profile.contact fields
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,contact_key",
    [
        ("Full Name", "name"),
        ("Email address", "email"),
        ("Phone number", "phone"),
        ("Current location", "location"),
        ("Personal website or portfolio", "website"),
        ("LinkedIn profile URL", "linkedin"),
        ("GitHub profile", "github"),
    ],
)
def test_deterministic_contact_fields(label, contact_key):
    q = _q(label)
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer(_PROFILE["contact"][contact_key], f"profile:contact.{contact_key}")


def test_deterministic_falls_through_when_contact_field_missing():
    # profile has no "website" key at all -- must not fabricate one.
    q = _q("Personal website or portfolio")
    out = answer_question(q, _PROFILE_NO_WEBSITE, _ANSWERS)
    assert out is None  # no client provided, nothing to fall back on


def test_deterministic_beats_llm_client_never_called():
    client = _FakeClient(["SHOULD-NOT-BE-USED"])
    q = _q("Email address")
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out == Answer(_PROFILE["contact"]["email"], "profile:contact.email")
    assert client.calls == 0


# ---------------------------------------------------------------------------
# EEO / demographic policy tier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label",
    [
        "Gender",
        "Race / Ethnicity",
        "Veteran status",
        "Disability status",
        "Sexual orientation",
        "Preferred pronouns",
    ],
)
def test_eeo_with_decline_option_selects_it(label):
    q = _q(label, kind="select", options=("Male", "Female", "Non-binary", "Prefer not to answer"))
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer("Prefer not to answer", "policy:eeo-decline")


@pytest.mark.parametrize(
    "decline_text",
    ["I don't wish to answer", "Decline to state", "I would rather not say", "Choose not to disclose"],
)
def test_eeo_decline_option_variants_are_recognized(decline_text):
    q = _q("Gender", kind="select", options=("Male", "Female", decline_text))
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer(decline_text, "policy:eeo-decline")


def test_eeo_required_without_decline_option_returns_none():
    q = _q("Veteran status", kind="select", required=True, options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out is None


def test_eeo_optional_without_decline_option_skips():
    q = _q("Disability status", kind="select", required=False, options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer("", "policy:skip")


def test_eeo_with_no_options_at_all_required_returns_none():
    q = _q("Gender identity (free text)", kind="text", required=True, options=())
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out is None


def test_eeo_never_reaches_the_llm():
    client = _FakeClient(["SHOULD-NOT-BE-USED"])
    q = _q("Gender", kind="select", required=True, options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out is None
    assert client.calls == 0


# ---------------------------------------------------------------------------
# LLM tier
# ---------------------------------------------------------------------------

def test_llm_tier_used_when_no_deterministic_match():
    client = _FakeClient(["We are a fully remote team, so no office days."])
    q = _q("How many days per week are you willing to come to the office?")
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out == Answer("We are a fully remote team, so no office days.", "llm:grounded")
    assert client.calls == 1


def test_llm_tier_unknown_required_returns_none():
    client = _FakeClient(["UNKNOWN"])
    q = _q("What is your favorite color?", required=True)
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out is None


def test_llm_tier_unknown_optional_returns_skip():
    client = _FakeClient(["UNKNOWN"])
    q = _q("What is your favorite color?", required=False)
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out == Answer("", "policy:skip")


def test_llm_tier_matches_option_case_insensitively():
    client = _FakeClient(["yes"])
    q = _q("Can you work full time?", kind="radio", options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out == Answer("Yes", "llm:grounded")  # exact option casing preserved


def test_llm_tier_option_mismatch_collapses_to_unknown_required():
    client = _FakeClient(["Maybe, depends on the offer"])
    q = _q("Can you work full time?", kind="radio", required=True, options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out is None


def test_llm_tier_option_mismatch_optional_skips():
    client = _FakeClient(["Maybe, depends on the offer"])
    q = _q("Can you work full time?", kind="radio", required=False, options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _ANSWERS, client=client)
    assert out == Answer("", "policy:skip")


def test_llm_messages_include_profile_answers_and_question():
    q = _q("Why do you want this role?")
    messages = build_llm_messages(q, _PROFILE, _ANSWERS)
    user = messages[1]["content"]
    assert "Ada Lovelace" in user
    assert "Not open to relocation" in user
    assert "Why do you want this role?" in user
    assert "UNKNOWN" in messages[0]["content"]  # honesty guard lives in the system prompt


def test_llm_messages_list_options_block():
    q = _q("Can you work full time?", kind="radio", options=("Yes", "No"))
    user = build_llm_messages(q, _PROFILE, _ANSWERS)[1]["content"]
    assert "- Yes" in user
    assert "- No" in user


def test_llm_messages_omit_options_block_when_no_options():
    q = _q("Why do you want this role?")
    user = build_llm_messages(q, _PROFILE, _ANSWERS)[1]["content"]
    assert "Options" not in user


# ---------------------------------------------------------------------------
# client=None: deterministic tier only
# ---------------------------------------------------------------------------

def test_no_client_no_deterministic_match_returns_none():
    q = _q("Why do you want this role?")
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out is None


def test_no_client_optional_no_deterministic_match_still_returns_none():
    # Per contract: client=None means deterministic tier only, full stop --
    # not conditioned on required/optional.
    q = _q("Why do you want this role?", required=False)
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out is None


# ---------------------------------------------------------------------------
# Adversarial probes (fix round 1). These use the REAL answers.yaml values so
# the outcomes match what a live form would produce. Every probe below is a
# thing the OLD screener got wrong (misroute, wrong yes/no, currency confusion,
# EEO recall/leak). "Wrong output = a lie to an employer" -- these are the gate.
# ---------------------------------------------------------------------------

_FULL_ANSWERS = {
    "salary_fulltime_gross_eur_month": 4700,
    "salary_fulltime_net_eur_month": "2100-2600",
    "hourly_rate_min_eur": 18,
    "availability_parttime": "Wednesday evenings and weekends",
    "work_authorization": (
        "EU citizen (test). Can work as an employee for any company hiring in "
        "the EU, or as an independent contractor."
    ),
    "notice_period": "approximately 45 working days (test fixture)",
    "relocation": "Open to discussing relocation for the right role; strong preference for full remote.",
}


# P1: "capacity" must not route to city/location (substring bug).
def test_probe_p1_capacity_routes_nowhere():
    q = _q("In what capacity did you previously work overtime?", kind="radio", options=("Full", "Partial"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


# P2: "home-based" must not route to location (bare "based" substring bug).
def test_probe_p2_home_based_not_location():
    q = _q("Is a home-based schedule acceptable to you?", kind="radio", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


# P3: US authorization -> "No" (non-EU jurisdiction, authorized-style polarity).
def test_probe_p3_us_authorized_maps_to_no():
    q = _q("Are you authorized to work in the US?", kind="select", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("No", "answers:work_authorization")


# P4: US sponsorship -> "Yes" (non-EU jurisdiction, sponsorship-style polarity).
def test_probe_p4_us_sponsorship_maps_to_yes():
    q = _q("Will you require visa sponsorship to work in the United States?", kind="select", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("Yes", "answers:work_authorization")


# P5: salary asked in USD -> fail closed (never type the EUR figure into a USD field).
def test_probe_p5_salary_usd_number_required_none():
    q = _q("What is your expected monthly salary in USD?", kind="number", required=True)
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


def test_probe_p5_salary_usd_number_optional_skips():
    q = _q("What is your expected monthly salary in USD?", kind="number", required=False)
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("", "policy:skip")


# P6: "portfolio project" must not route to the website field (substring bug).
def test_probe_p6_portfolio_project_not_website():
    q = _q("Describe a portfolio project you are proud of.", kind="textarea", required=True)
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


# P7: "willing to relocate?" Yes/No -> "Yes" (open-to-discussing maps to yes-like).
def test_probe_p7_relocation_radio_maps_to_yes():
    q = _q("Are you willing to relocate?", kind="radio", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("Yes", "answers:relocation")


# P8: "race conditions" is a concurrency question, NOT EEO.
def test_probe_p8_race_conditions_not_eeo():
    q = _q("How do you avoid race conditions in concurrent code?", kind="textarea", required=True)
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


# P9: Hispanic/Latino demographic with a decline option -> decline (recall).
def test_probe_p9_hispanic_latino_declines():
    q = _q("Are you Hispanic or Latino?", kind="select", options=("Yes", "No", "I prefer not to answer"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("I prefer not to answer", "policy:eeo-decline")


# P10: bare "Sex" with a decline option -> decline (word-boundary recall).
def test_probe_p10_bare_sex_declines():
    q = _q("Sex", kind="select", options=("Male", "Female", "Prefer not to say"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("Prefer not to say", "policy:eeo-decline")


# P11: "prior notice of criminal proceedings" must not route to notice_period.
def test_probe_p11_criminal_notice_not_notice_period():
    q = _q("Have you received prior notice of criminal proceedings?", kind="radio", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


# P12: "cloud-based" must not route to location (bare "based" substring bug).
def test_probe_p12_cloud_based_not_location():
    q = _q("Are you comfortable with a cloud-based workflow?", kind="radio", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


# EU authorization -> "Yes" (EU jurisdiction, authorized-style polarity).
def test_probe_eu_authorized_maps_to_yes():
    q = _q("Are you authorized to work in the EU?", kind="select", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("Yes", "answers:work_authorization")


# Salary in a number field with EUR explicit -> the bare number.
def test_probe_salary_eur_explicit_number_bare():
    q = _q("Expected monthly salary in EUR?", kind="number", required=True)
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) == Answer("4700", "answers:salary_fulltime_gross_eur_month")


# Salary in a text field, currency unspecified -> a value that STATES EUR.
def test_probe_salary_unspecified_text_states_currency():
    q = _q("What is your expected monthly salary?", kind="text", required=True)
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert out.grounded_in == "answers:salary_fulltime_gross_eur_month"
    assert "EUR" in out.value
    assert "4700" in out.value


# Relocation that declines -> maps to the No-like option.
def test_probe_relocation_radio_maps_to_no_when_declined():
    ans = {**_FULL_ANSWERS, "relocation": "Not open to relocation; fully remote only."}
    q = _q("Are you willing to relocate?", kind="radio", options=("Yes", "No"))
    assert answer_question(q, _PROFILE, ans) == Answer("No", "answers:relocation")


# Number gate: notice ("approximately 20 working days") is NOT a bare number.
def test_probe_notice_number_kind_fails_closed():
    q = _q("How many days is your notice period?", kind="number", required=True)
    assert answer_question(q, _PROFILE, _FULL_ANSWERS) is None


# LLM DECLINE backstop: a demographic question that slips past the deterministic
# EEO tier and the model replies DECLINE -> we pick the decline option.
def test_probe_llm_decline_backstop_picks_decline_option():
    client = _FakeClient(["DECLINE"])
    q = _q("Please indicate your marital status.", kind="select", required=True,
           options=("Single", "Married", "Prefer not to say"))
    out = answer_question(q, _PROFILE, _FULL_ANSWERS, client=client)
    assert out == Answer("Prefer not to say", "policy:eeo-decline")
    assert client.calls == 1


# LLM DECLINE backstop with no decline option available on a required question.
def test_probe_llm_decline_backstop_no_option_required_none():
    client = _FakeClient(["DECLINE"])
    q = _q("Please state your marital status.", kind="text", required=True)
    out = answer_question(q, _PROFILE, _FULL_ANSWERS, client=client)
    assert out is None
    assert client.calls == 1


# ---------------------------------------------------------------------------
# Fix round 2 probes.
# ---------------------------------------------------------------------------

# Finding 1: "tell us" / "let us" pronoun must not resolve to USA jurisdiction.
def test_probe_tell_us_pronoun_not_usa_yes():
    q = _q("Please tell us whether you are authorized to work.", kind="select", options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert out is None  # no jurisdiction detected -> falls to LLM tier -> None with client=None


def test_probe_let_us_know_pronoun_not_usa():
    q = _q("Are you authorized to work? Please let us know.", kind="select", options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert out is None


# Real "US" jurisdiction must still resolve deterministically.
def test_probe_authorized_in_the_us_still_resolves_no():
    q = _q("Are you authorized to work in the US?", kind="select", options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert out == Answer("No", "answers:work_authorization")


def test_probe_authorized_for_the_us_market_still_resolves_no():
    q = _q("Are you authorized to work for the US market?", kind="select", options=("Yes", "No"))
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert out == Answer("No", "answers:work_authorization")


# Finding 2: "network engineer" / "internet" must not misroute gross->net band.
def test_probe_network_engineer_salary_is_gross_not_net():
    q = _q("What salary do you expect as a network engineer?")
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert "gross" in out.value
    assert "net" not in out.value


# Finding 3: LLM tier must run the same kind gate as the deterministic tier,
# so a select field with no options doesn't accept raw LLM prose.
def test_probe_llm_select_no_options_required_returns_none():
    client = _FakeClient(["Blue"])
    q = _q("Favorite color?", kind="select", required=True, options=())
    out = answer_question(q, _PROFILE, _FULL_ANSWERS, client=client)
    assert out is None


def test_probe_llm_select_no_options_optional_skips():
    client = _FakeClient(["Blue"])
    q = _q("Favorite color?", kind="select", required=False, options=())
    out = answer_question(q, _PROFILE, _FULL_ANSWERS, client=client)
    assert out == Answer("", "policy:skip")


# Finding 4: EEO recall for "dob" / "birth date" (reversed order from "date of birth").
def test_probe_eeo_dob_declines():
    q = _q("DOB", kind="select", options=("18-25", "26-35", "Prefer not to say"))
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert out == Answer("Prefer not to say", "policy:eeo-decline")


def test_probe_eeo_birth_date_declines():
    q = _q("Birth date", kind="select", options=("18-25", "26-35", "Prefer not to say"))
    out = answer_question(q, _PROFILE, _FULL_ANSWERS)
    assert out == Answer("Prefer not to say", "policy:eeo-decline")


# ---------------------------------------------------------------------------
# Current company/employer (Lever's `org` field). Grounded in
# profile.experiences, not profile.contact -- and must skip self-employment
# (Founder) / freelance entries in favor of a genuine employer, mirroring
# Teodor's real profile.yaml shape (Ministeru' Creativ founder + Wolff
# freelance + EA employee, all three dated "... - Present").
# ---------------------------------------------------------------------------

_PROFILE_MULTI_CURRENT = {
    **_PROFILE,
    "experiences": [
        {"id": "agency", "role": "Founder", "company": "Acme Creative",
         "dates": "Mar 2026 - Present"},
        {"id": "gig", "role": "Content Producer (freelance, ongoing)",
         "company": "Freelance Studio", "dates": "Jan 2026 - Present"},
        {"id": "job", "role": "Assistant Engineer", "company": "Big Corp",
         "dates": "Aug 2024 - Present"},
        {"id": "old", "role": "Intern", "company": "Old Co",
         "dates": "Jan 2020 - Dec 2021"},
    ],
}

_PROFILE_NO_EXPERIENCES = {**_PROFILE, "experiences": []}


@pytest.mark.parametrize("label", ["Current company", "Current employer", "Present employer"])
def test_current_company_grounds_in_the_genuine_employer_not_founder_or_freelance(label):
    q = _q(label)
    out = answer_question(q, _PROFILE_MULTI_CURRENT, _ANSWERS)
    assert out == Answer("Big Corp", "profile:experiences.current")


def test_current_company_no_experiences_returns_none():
    q = _q("Current company")
    assert answer_question(q, _PROFILE_NO_EXPERIENCES, _ANSWERS) is None


def test_current_company_only_self_employed_present_entries_returns_none():
    profile = {
        **_PROFILE,
        "experiences": [
            {"id": "agency", "role": "Founder", "company": "Acme Creative", "dates": "Mar 2026 - Present"},
        ],
    }
    q = _q("Current company")
    assert answer_question(q, profile, _ANSWERS) is None


def test_current_company_falls_through_to_llm_when_no_genuine_employer():
    """No grounded experience -> client=None returns None (never guesses);
    with a client, the LLM tier gets a shot, same as any other unmatched
    deterministic category."""
    profile = {
        **_PROFILE,
        "experiences": [
            {"id": "agency", "role": "Founder", "company": "Acme Creative", "dates": "Mar 2026 - Present"},
        ],
    }
    client = _FakeClient(["Acme Creative (self-employed)"])
    q = _q("Current company")
    out = answer_question(q, profile, _ANSWERS, client=client)
    assert out == Answer("Acme Creative (self-employed)", "llm:grounded")
    assert client.calls == 1


def test_current_company_select_option_mismatch_fails_closed_required():
    q = _q("Current company", kind="select", required=True, options=("Google", "Meta"))
    out = answer_question(q, _PROFILE_MULTI_CURRENT, _ANSWERS)
    assert out is None


def test_current_company_never_confused_with_generic_company_name_routing():
    """A plain "Company name" question (no "current"/"employer" wording) must
    never route to the current-employer tier -- "company name" is a generic
    field (references, prior employer history, etc.), not specifically "my
    own present employer". It happens to match profile.contact.name instead
    (pre-existing \\bname\\b routing, unrelated to this tier) -- the point
    here is only that it is NOT grounded in profile.experiences.current."""
    q = _q("Company name")
    out = answer_question(q, _PROFILE_MULTI_CURRENT, _ANSWERS)
    assert out != Answer("Big Corp", "profile:experiences.current")
