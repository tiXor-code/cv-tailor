import json
from unittest.mock import MagicMock

from cv_tailor.match import score_job, SCORER_SYSTEM_PROMPT


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
