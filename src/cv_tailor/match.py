"""LLM-driven fit scoring: rate a JobPosting against the candidate profile."""
import json
import os
import yaml
from typing import Any


SCORER_SYSTEM_PROMPT = """You are scoring how well a job posting fits the candidate Teodor-Cristian Lutoiu.

The candidate's profile is provided. The job posting is provided.

Score the fit 0-10 where:
- 9-10 = ideal match: stack, seniority, location, and role focus all align
- 7-8 = strong match: most align, minor stretches
- 5-6 = workable match: some alignment but real gaps
- 0-4 = poor match: skip

Heavily penalize:
- Seniority mismatches (the candidate is mid-senior, not staff/principal/PhD-track)
- Location mismatches (the candidate is in Bucharest, EU; needs remote or EU-local)
- Pure ML research roles requiring PhD or ML model training (not the candidate's profile)
- Sales/Marketing/Customer Success/Design roles
- Already-experienced-in-bank-FSI-only roles
- Games-industry roles (the candidate is pivoting AWAY from games)

Heavily favor:
- AI engineering, AI automation, agentic systems, LLM orchestration roles
- Python + TypeScript stacks
- Remote-first EU roles
- Forward-deployed / solutions-engineer / internal-tooling AI roles
- n8n, RAG, eval frameworks, MCP, agentic workflows in the JD

Return strict JSON:
{
  "score": 0-10 integer,
  "reason": "one-sentence reason (under 30 words)",
  "key_keywords_matched": ["..."]
}

Return ONLY the JSON, no prose."""


def score_job(profile: dict, job_title: str, job_location: str, job_description: str,
              *, client: Any, deployment: str | None = None) -> dict:
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    user = (
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n"
        f"# Job posting\nTitle: {job_title}\nLocation: {job_location}\n\nDescription:\n{job_description[:6000]}"
    )
    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": SCORER_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)
