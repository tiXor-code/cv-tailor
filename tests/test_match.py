import json
from unittest.mock import MagicMock

from cv_tailor.match import score_job, SCORER_SYSTEM_PROMPT, CONTENT_TRACK_ADDENDUM


def test_scorer_system_prompt_mentions_candidate_and_constraints():
    p = SCORER_SYSTEM_PROMPT
    assert "Teodor" in p
    assert "Bucharest" in p or "EU" in p
    assert "JSON" in p


def test_score_job_calls_llm_with_profile_and_jd():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = json.dumps({
        "score": 8,
        "reason": "AI engineering, remote EU, Python/TS stack",
        "key_keywords_matched": ["LLM", "Python", "remote"],
    })
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response

    profile = {"contact": {"name": "Teodor"}, "skills": {"core": ["Python"]}}
    result = score_job(
        profile,
        "Senior AI Engineer",
        "Europe (Remote)",
        "We need a Python+LLM engineer to build agentic workflows.",
        client=client,
        deployment="gpt-4o-mini",
    )

    assert result["score"] == 8
    assert "LLM" in result["key_keywords_matched"]

    client.chat.completions.create.assert_called_once()
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-mini"
    assert kwargs["temperature"] == 0.2
    assert kwargs["response_format"] == {"type": "json_object"}

    msgs = kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert "Teodor" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    # The user message must include the profile yaml AND the JD title/desc.
    assert "contact" in msgs[1]["content"]  # profile yaml present
    assert "Senior AI Engineer" in msgs[1]["content"]
    assert "agentic workflows" in msgs[1]["content"]
    assert "Europe (Remote)" in msgs[1]["content"]


def test_score_job_truncates_long_descriptions():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = json.dumps(
        {"score": 5, "reason": "ok", "key_keywords_matched": []}
    )
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response

    long_desc = "x" * 20000
    score_job({"contact": {}}, "T", "L", long_desc, client=client, deployment="m")
    msgs = client.chat.completions.create.call_args.kwargs["messages"]
    # User content shouldn't carry all 20k chars of x's
    assert msgs[1]["content"].count("x") <= 6500


def _fake_client_capturing():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = json.dumps(
        {"score": 5, "reason": "ok", "key_keywords_matched": []}
    )
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response
    return client


def test_score_job_ai_track_prompt_is_byte_stable():
    """track='ai' (the default) must send SCORER_SYSTEM_PROMPT unchanged -- no
    content-track addendum leaks into the AI track's scoring calls."""
    client = _fake_client_capturing()
    score_job({"contact": {}}, "T", "L", "desc", client=client, deployment="m")
    msgs = client.chat.completions.create.call_args.kwargs["messages"]
    assert msgs[0]["content"] == SCORER_SYSTEM_PROMPT

    client2 = _fake_client_capturing()
    score_job({"contact": {}}, "T", "L", "desc", client=client2, deployment="m", track="ai")
    msgs2 = client2.chat.completions.create.call_args.kwargs["messages"]
    assert msgs2[0]["content"] == SCORER_SYSTEM_PROMPT


def test_scorer_system_prompt_has_remote_reality_check():
    """Real incident: a SerpAPI-cross-listed 'Remote' card's true JD anchored
    the role to a hybrid US-onsite cadence in Raleigh/San Jose (Cisco). The
    scorer must not trust a bare 'Remote' label. Since 2026-09-24 (Teodor:
    remote only, any country that will hire him) the cap is about onsite work
    and residency/authorization, no longer about the US as such."""
    low = SCORER_SYSTEM_PROMPT.lower()
    assert "reality check" in low or "reality-check" in low
    assert "hybrid" in low and "days per week" in low
    assert "work authorization" in low or "work-authorization" in low
    assert "security clearance" in low
    assert "anywhere" in low
    assert "3 or lower" in low
    assert "needs remote or eu-local" not in low, "EU-only framing is retired"


def test_scorer_prompt_carries_the_2026_09_24_calibration():
    low = SCORER_SYSTEM_PROMPT.lower()
    assert "forward deployed" in low
    # Round 2 (his 40 marks): he said Yes to Senior, Staff and Lead AI roles.
    # Seniority and stated years are not penalized.
    assert "do not penalize seniority" in low
    assert "claude code" in low                # AI-native builder roles are the ideal
    assert "startup" in low                    # startups win ties
    assert "german" in low and "french" in low
    assert "gambling" in low                   # explicitly fine
    assert "no ai" in low                      # AI-related roles only


def test_the_pay_floor_travels_in_the_user_message_never_the_prompt():
    """The floor is his real number (answers.yaml, gitignored). It must never
    be written into this public repo's prompt, so it rides in per call."""
    client = _fake_client_capturing()
    score_job({"contact": {}}, "T", "L", "desc", client=client, deployment="m",
              min_monthly_eur=1234)
    msgs = client.chat.completions.create.call_args.kwargs["messages"]
    assert msgs[0]["content"] == SCORER_SYSTEM_PROMPT
    assert "1234 EUR gross per month" in msgs[1]["content"]


def test_no_pay_floor_means_no_pay_line():
    client = _fake_client_capturing()
    score_job({"contact": {}}, "T", "L", "desc", client=client, deployment="m")
    assert "gross per month" not in client.chat.completions.create.call_args.kwargs["messages"][1]["content"]


def test_score_job_content_track_prompt_has_availability_constraint():
    client = _fake_client_capturing()
    score_job({"contact": {}}, "T", "L", "desc", client=client, deployment="m", track="content")
    msgs = client.chat.completions.create.call_args.kwargs["messages"]
    system = msgs[0]["content"]

    # Base prompt still present -- the content track adds to it, doesn't replace it.
    assert system.startswith(SCORER_SYSTEM_PROMPT)
    assert system == SCORER_SYSTEM_PROMPT + CONTENT_TRACK_ADDENDUM

    assert "Monday" in system
    assert "Friday" in system
    assert "weekends" in system
    assert "content-producer" in system
    assert "4" in system  # full-time content roles capped at <= 4


def test_scorer_prompt_carries_round_two_hard_nos():
    """From his notes: hybrid is a hard pass, non-English-friendly is a no,
    frequent travel and specialisms he lacks (security, medical, data
    engineering, MLOps infrastructure) are no's, non-technical roles are out."""
    low = SCORER_SYSTEM_PROMPT.lower()
    assert "hybrid" in low and "2 or lower" in low
    assert "other than english or romanian" in low
    assert "frequent travel" in low
    for word in ("security", "medical", "data engineering", "mlops"):
        assert word in low, word
    assert "account executive" in low or "sales" in low


def test_ai_native_development_is_the_top_priority():
    """Teodor, 2026-09-24, after rating Ygo: "focus more on jobs like the Ygo
    one where the focus is on claude code and ai fast development". AI-native
    signals must rank above plain AI engineering, which is capped below them."""
    low = SCORER_SYSTEM_PROMPT.lower()
    assert "top priority" in low
    for signal in ("claude code", "cursor", "ai-assisted", "ai-first", "vibe coding"):
        assert signal in low, signal
    assert "at most 7" in low


# --- AI-native ranking, enforced in code (2026-09-24). Measured on his 40
# marks: gpt-4o-mini ignored the prompt's "at most 7 without a signal" cap
# (8s for postings naming no AI tool) and gave Ygo -- two strong signals,
# his favourite -- the same 8. The ordering he asked for is applied here.

from cv_tailor.match import ai_native_signals, rank_ai_native


def test_signals_are_the_distinct_ai_native_markers():
    desc = ("You'll use Claude Code and Cursor to ship fast. This is not a traditional "
            "coding role. AI-assisted development all day.")
    assert set(ai_native_signals(desc)) == {"claude code", "cursor", "not a traditional coding role",
                                            "ai-assisted development"}
    assert ai_native_signals("A precursor to our platform. Plain Python backend.") == []


def test_no_signal_caps_at_seven():
    assert rank_ai_native(9, "Senior AI engineer, LLM agents, Python.") == 7
    assert rank_ai_native(5, "Senior AI engineer, LLM agents, Python.") == 5


def test_signals_lift_a_decent_ai_fit_above_plain_ai_roles():
    one = "Build agents. We use Cursor daily."
    two = "Build agents with Claude Code. Not a traditional coding role."
    assert rank_ai_native(8, one) == 9
    assert rank_ai_native(8, two) == 10
    assert rank_ai_native(9, two) == 10


def test_a_weak_fit_is_not_boosted_by_a_tool_mention():
    """A Java/React role that mentions Copilot is still a Java/React role."""
    assert rank_ai_native(3, "Java and React developer. Copilot licences provided.") == 3


def test_product_and_analyst_roles_are_out():
    """Asked about Constructor's Staff PM and Data Analyst (both his Yes on the
    rating page), Teodor said pass (2026-09-24)."""
    low = SCORER_SYSTEM_PROMPT.lower()
    assert "product manager, program manager, producer and data analyst roles score 4 or" in low
