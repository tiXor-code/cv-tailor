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

Location reality-check: a "Remote" label on the posting is not enough. Read the
description -- if it anchors the actual work to a US, Canada, Australia, or UK
office or team (hybrid schedules, "N days per week onsite/in office", named
US/Canada/Australia/UK cities or states as the work location, US-timezone-only
core hours, or US/Canada/Australia/UK work-authorization requirements), score
the role 3 or lower regardless of the "Remote" label, UNLESS the text
explicitly affirms remote eligibility from Europe/EMEA/anywhere (e.g. "remote
from anywhere", "EU-based candidates welcome", "open to EMEA/CET"). The
candidate lives in Bucharest, Romania and cannot commute to a US/UK/Canada/
Australia office.

Return strict JSON:
{
  "score": 0-10 integer,
  "reason": "one-sentence reason (under 30 words)",
  "key_keywords_matched": ["..."]
}

Return ONLY the JSON, no prose."""


# Appended to SCORER_SYSTEM_PROMPT only when track == "content". The "ai" track
# (default) sends SCORER_SYSTEM_PROMPT byte-unchanged -- see
# test_score_job_ai_track_prompt_is_byte_stable in tests/test_match.py.
CONTENT_TRACK_ADDENDUM = """

# Track: content
This posting belongs to Teodor's content/freelance track. Score it against his
content-producer background (his content-related summary_pool entries, experience,
and skills in the profile), not his AI-engineering background.

Hard availability constraint: Teodor is only available to work Monday, Friday, and
weekends.
- Part-time, freelance, or contract roles that fit this schedule score normally on
  the 0-10 scale above.
- Full-time (5-day-a-week employee) content roles do not fit his availability, no
  matter how strong the content-background match. Score these 4 or lower."""


def score_job(profile: dict, job_title: str, job_location: str, job_description: str,
              *, client: Any, deployment: str | None = None, track: str = "ai") -> dict:
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    system_prompt = SCORER_SYSTEM_PROMPT
    if track == "content":
        system_prompt = SCORER_SYSTEM_PROMPT + CONTENT_TRACK_ADDENDUM
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    user = (
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n"
        f"# Job posting\nTitle: {job_title}\nLocation: {job_location}\n\nDescription:\n{job_description[:6000]}"
    )
    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content)
