"""LLM-driven fit scoring: rate a JobPosting against the candidate profile."""
import json
import os
import yaml
from typing import Any


SCORER_SYSTEM_PROMPT = """You are scoring how well a job posting fits the candidate Teodor-Cristian Lutoiu.

The candidate's profile is provided. The job posting is provided.

Score the fit 0-10 where:
- 9-10 = ideal match: role focus, seniority, remote setup, and pay all align
- 7-8 = strong match: most align, minor stretches
- 5-6 = workable match: some alignment but real gaps
- 0-4 = poor match: skip

What he wants (his own calibration, 2026-09-24):
- AI-related roles ONLY. A role with no AI component in the actual work (no LLMs,
  agents, AI automation, ML, or AI integration) scores 4 or lower, however good
  the company.
- Best fit: forward deployed engineer, AI automation / implementation / solutions
  roles where he builds automations and agentic workflows, integrates AI into a
  business, and deploys fast -- customer-facing or internal-tooling. n8n, RAG, MCP,
  eval frameworks and agentic workflows in the JD are strong signals.
- Senior titles are fine WHEN the role is shaped like that (build, integrate,
  deploy fast). They are NOT fine when the posting demands many years of hands-on
  professional software engineering (5+ years of hands-on coding, deep CS or
  algorithms, low-level/distributed systems, staff/principal scope): score those
  4 or lower. He directs AI to build; he is not a career backend engineer.
- Startups win ties: seed to Series B, small teams, founding roles -- score a
  startup 1 point above an otherwise equal larger company.
- Any industry is fine, including iGaming, gambling, forex and fintech. The one
  exception: games-industry development roles (he is leaving games) score 3 or lower.
- Pure ML research roles needing a PhD or model training from scratch score 3 or lower.
- Sales, marketing, recruiting and design roles score 2 or lower.
- A role that requires German or French scores 2 or lower (he speaks neither).

Where and when he works: fully remote only, from Bucharest (EU citizen), for a
company in ANY country -- US, EU, Asia, Cyprus, anywhere that will hire him, as an
employee or as a contractor through his own company. Prefer roles on GMT/European
hours; US-hours overlap is acceptable, so only a posting that requires working
fully on US or Asian hours loses 1 point.

Remote reality check: a "Remote" label on the posting is not enough. Read the
description. Score the role 3 or lower, regardless of the "Remote" label, if it:
- anchors the work to an office (hybrid schedules, "N days per week onsite/in
  office", relocation required), or
- requires residency or work authorization in a specific country outside the EU
  (US, Canada, UK, Australia, ...), US-only hiring, or a security clearance,
UNLESS the text explicitly affirms remote eligibility for him (e.g. "remote from
anywhere", "worldwide", "EMEA", "EU-based candidates welcome", "contractors
worldwide"). A job listed as "Remote - <country>" with no stated restriction is
not disqualified by the listing alone -- judge the text.

Pay: the user message may give his minimum pay. If the posting STATES pay and even
the top of its range is clearly below that minimum (convert currency and
period -- annual vs monthly -- before comparing), score it 3 or lower. A posting
that states no pay is not penalized.

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
              *, client: Any, deployment: str | None = None, track: str = "ai",
              min_monthly_eur: int | None = None) -> dict:
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    system_prompt = SCORER_SYSTEM_PROMPT
    if track == "content":
        system_prompt = SCORER_SYSTEM_PROMPT + CONTENT_TRACK_ADDENDUM
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    # His real floor comes from answers.yaml (gitignored) at call time. It must
    # never be written into SCORER_SYSTEM_PROMPT: this repo is public.
    pay = (f"# Candidate's minimum pay\n{min_monthly_eur} EUR gross per month\n\n"
           if min_monthly_eur else "")
    user = (
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n{pay}"
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
