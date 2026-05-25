import json
from unittest.mock import MagicMock
from cv_tailor.tailor_llm import build_messages, tailor

def test_build_messages_includes_profile_and_jd():
    profile = {"contact": {"name": "T"}}
    jd = "We want a Python engineer."
    msgs = build_messages(profile, jd)
    assert msgs[0]["role"] == "system"
    assert "honesty" in msgs[0]["content"].lower() or "do not invent" in msgs[0]["content"].lower()
    assert msgs[1]["role"] == "user"
    assert "Python engineer" in msgs[1]["content"]
    assert "contact" in msgs[1]["content"]  # profile yaml present

def test_tailor_calls_client_and_returns_parsed_json():
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = json.dumps({
        "job_meta": {"company": "X", "role": "Y", "location": None, "jd_url": None,
                      "seniority_signal": "mid"},
        "chosen_summary_id": "default",
        "summary_rewrite": "...",
        "experience_ids_ordered": [],
        "experience_bullets": {},
        "project_ids": [],
        "skills_emphasis": [],
        "jd_keywords_matched": [],
        "gaps_honest": [],
        "one_line_pitch": "..."
    })
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response

    result = tailor({"contact": {"name": "T"}}, "JD text", client=client,
                    deployment="gpt-4o-mini")

    assert result["chosen_summary_id"] == "default"
    client.chat.completions.create.assert_called_once()
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-4o-mini"
    assert call_kwargs["temperature"] == 0.2
    assert call_kwargs["response_format"] == {"type": "json_object"}
