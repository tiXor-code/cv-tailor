"""Single Azure OpenAI call that turns (profile, JD) into structured fields.json.

The system prompt encodes the honesty guard (no invention) and the exact JSON
schema the caller expects. The caller passes its own openai client so we can
inject a mock in tests.
"""
import json
import os
import yaml
from typing import Any

SYSTEM_PROMPT = """You are a CV tailoring assistant for Teodor-Cristian Lutoiu.

You receive the candidate's full canonical profile (YAML) and a job description (text).
You output a single JSON object that drives a deterministic CV renderer.

HONESTY RULES — never break these:
- Only pick experience_ids, project_ids, skills, and bullet indices that EXIST in the profile.
- Do not invent companies, roles, dates, projects, or skills.
- summary_rewrite must use only facts present in profile.yaml; you may re-phrase and
  re-emphasize, but you may not add new facts.
- If the JD requires something the profile does not contain, list it under gaps_honest.
- Prefer fewer but stronger items over padding with weak ones.

OUTPUT SCHEMA (strict — return exactly this JSON):
{
  "job_meta": {
    "company": "string",
    "role": "string",
    "location": "string or null",
    "jd_url": "string or null",
    "seniority_signal": "junior | mid | senior | lead | unspecified"
  },
  "chosen_summary_id": "string (one id from profile.summary_pool)",
  "summary_rewrite": "string (2-3 sentences, tailored, profile-grounded only)",
  "experience_ids_ordered": ["string", "..."],
  "experience_bullets": { "<experience_id>": [0, 2, 3] },
  "project_ids": ["string", "..."],
  "skills_emphasis": ["string from profile.skills"],
  "jd_keywords_matched": ["string from JD"],
  "gaps_honest": ["string"],
  "one_line_pitch": "string"
}

Return ONLY the JSON, no surrounding prose.
"""


def build_messages(profile: dict, jd_text: str) -> list[dict]:
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    user_content = (
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n"
        f"# Job description\n```\n{jd_text}\n```"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def tailor(
    profile: dict,
    jd_text: str,
    *,
    client: Any,
    deployment: str | None = None,
) -> dict:
    """Call the LLM and return the parsed JSON."""
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    response = client.chat.completions.create(
        model=deployment,
        messages=build_messages(profile, jd_text),
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    return json.loads(raw)


def build_azure_client():
    """Construct an Azure OpenAI client from env vars. Imported lazily so
    tests can run without the SDK configured."""
    from openai import AzureOpenAI

    return AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    )
