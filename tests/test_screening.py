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
    q = _q("What is your expected monthly salary?")
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer(
        str(_ANSWERS["salary_fulltime_gross_eur_month"]), "answers:salary_fulltime_gross_eur_month"
    )


def test_deterministic_salary_net_when_label_says_net():
    q = _q("What is your expected net monthly salary?")
    out = answer_question(q, _PROFILE, _ANSWERS)
    assert out == Answer(
        str(_ANSWERS["salary_fulltime_net_eur_month"]), "answers:salary_fulltime_net_eur_month"
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
