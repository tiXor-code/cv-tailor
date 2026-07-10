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


def test_scorer_system_prompt_has_us_anchor_reality_check():
    """Real incident: a SerpAPI-cross-listed 'Remote' card's true JD anchored
    the role to a hybrid US-onsite cadence in Raleigh/San Jose (Cisco). The
    scorer must not trust a bare 'Remote' label -- it needs its own reality
    check, mirroring norina-jobs' scorer hardening."""
    p = SCORER_SYSTEM_PROMPT
    low = p.lower()
    assert "reality-check" in low or "reality check" in low
    assert "hybrid" in low
    assert "onsite" in low or "on-site" in low
    assert "days per week" in low
    assert "work-authorization" in low or "work authorization" in low
    assert "remote from anywhere" in low or "anywhere" in low
    assert "emea" in low
    # The hard cap: score 3 or lower regardless of the "Remote" label.
    assert "3 or lower" in low


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
