"""LLM-driven fit scoring: rate a JobPosting against the candidate profile."""
import json
import os
import re
import yaml
from typing import Any


SCORER_SYSTEM_PROMPT = """You are scoring how well a job posting fits the candidate Teodor-Cristian Lutoiu.

The candidate's profile is provided. The job posting is provided.

Score the fit 0-10 where:
- 9-10 = ideal match: role focus, seniority, remote setup, and pay all align
- 7-8 = strong match: most align, minor stretches
- 5-6 = workable match: some alignment but real gaps
- 0-4 = poor match: skip

What he wants (calibrated 2026-09-24 against 40 postings he rated himself):
- TOP PRIORITY, 9-10: AI-native development roles, where the job itself is
  shipping fast WITH AI tools. Signals: the posting names Claude Code, Cursor,
  Copilot, Codex or similar; asks for AI-assisted or AI-first development,
  "vibe coding", or "think how can AI build this"; says it is "not a traditional
  coding role"; or rewards speed and ownership over hand-written code. Real example he
  loved: a product engineer role that "uses Claude Code and other AI tools to ship
  fast". Two or more such signals = 9-10; one clear signal = 8-9.
- Good, but at most 7 when the posting shows NO AI-native signal: AI engineer,
  applied AI, LLM/agent engineer, forward deployed engineer, AI solutions/
  automation/implementation roles, AI enablement or AI coaching roles that teach
  teams agentic/AI-assisted engineering, and any technical role with "agentic" or
  "AI" in the title. Backend roles at a company whose core product IS AI or
  AI-powered search/recommendations count too.
- Do not penalize seniority: Senior, Staff, Lead and engineering-manager titles
  and stated years of experience are fine -- he applies to them.
- The tech stack is NEVER a gap. He builds with AI tools, so a required
  programming language, framework or database (Go, Golang, GraphQL, Rust, Java,
  TypeScript, Kubernetes, ...) must not lower the score and must not be named as a
  gap in the reason.
- Specialisms outside his world score 4 or lower even when AI is mentioned:
  security engineering/research, medical or biosignal/scientific algorithms,
  data engineering and data pipelines, MLOps infrastructure/versioning, embedded, and
  pure traditional stacks with no AI component (e.g. a Java/React developer role).
- Non-technical roles (sales, account executive, customer success, marketing/GTM,
  recruiting, design) score 2 or lower.
- Product manager, program manager, producer and data analyst roles score 4 or
  lower: he passed on them when asked (2026-09-24), even on AI products.
- Startups win ties: seed to Series B, small teams, founding roles -- score a
  startup 1 point above an otherwise equal larger company.
- Any industry is fine, including iGaming, gambling, forex and fintech. The one
  exception: games-industry development roles (he is leaving games) score 3 or lower.
- Pure ML research roles needing a PhD or model training from scratch score 3 or lower.
- A role that requires any language other than English or Romanian (German,
  French, Dutch, Greek, Spanish, ...), or a posting aimed at a local-language
  market, scores 2 or lower. Another language listed only as a plus is fine.
- A role with frequent travel scores 4 or lower.

Where and when he works: fully remote only, from Bucharest (EU citizen), for a
company in ANY country -- US, EU, Asia, Cyprus, anywhere that will hire him, as an
employee or as a contractor through his own company. Prefer roles on GMT/European
hours; US-hours overlap is acceptable, so only a posting that requires working
fully on US or Asian hours loses 1 point.

Remote reality check: a "Remote" label on the posting is not enough. Read the
whole description, perks included. Hybrid is a hard no: any hybrid work model,
office days or office-based setup -- even one mentioned only in the benefits list,
e.g. "hybrid work and flexible hours" -- scores 2 or lower. Score the role 3 or
lower, regardless of the "Remote" label, if it:
- anchors the work to an office ("N days per week onsite/in office", relocation
  required), or
- requires residency or work authorization in a specific country outside the EU
  (US, Canada, UK, Australia, ...), US-only hiring, or a security clearance --
  including indirectly, e.g. a perk like "work outside Canada for up to 90 days
  a year" means he would have to live in Canada,
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


# --- AI-native ranking (Teodor, 2026-09-24: "focus more on jobs like the Ygo
# one where the focus is on claude code and ai fast development") ------------
# Enforced in code because the scorer model did not follow it from the prompt:
# on his 40 rated postings it scored postings naming no AI tool at 8 and his
# favourite (two strong signals) at the same 8.
_AI_NATIVE_PATTERNS = {
    "claude code": r"\bclaude\s+code\b",
    "cursor": r"\bcursor\b(?!\s+(?:position|pagination|based))",
    # Copilot is also a Microsoft product name ("ChatGPT, Gemini and Copilot
    # results"): only GitHub Copilot, or Copilot next to coding words, counts.
    "copilot": r"github\s+copilot|(?:cod(?:e|ing)|program\w*|develop\w*)\s+(?:\w+\s+){0,3}copilot|"
               r"copilot\s+(?:\w+\s+){0,3}(?:cod(?:e|ing)|pair|ide|autocomplete)",
    "codex": r"\bcodex\b",
    "windsurf": r"\bwindsurf\b",
    "ai-assisted development": r"\bai[- ]assisted\s+(?:development|coding|engineering|programming)",
    "ai-first development": r"\bai[- ]first\s+(?:development|engineering|engineer|coding|builder)",
    "ai-native engineering": r"\bai[- ]native\s+(?:development|engineering|engineer|builder|workflow)",
    "vibe coding": r"\bvibe[- ]cod",
    "not a traditional coding role": r"not\s+a\s+traditional\s+(?:coding|engineering|developer)",
    "how can ai build this": r"how\s+can\s+ai\s+build",
}
_AI_NATIVE_RES = {k: re.compile(v, re.I) for k, v in _AI_NATIVE_PATTERNS.items()}
AI_NATIVE_CAP_WITHOUT_SIGNAL = 7
AI_NATIVE_MIN_FIT = 5  # below this the LLM judged it a poor AI fit: no boost


def ai_native_signals(description: str) -> list[str]:
    """Distinct AI-native development markers in a posting."""
    text = description or ""
    return [name for name, rx in _AI_NATIVE_RES.items() if rx.search(text)]


def rank_ai_native(score: int, description: str) -> int:
    """Final score: postings with no AI-native signal cap at 7; a decent AI
    fit (>= 5) gains +1 for one signal, +2 for two or more, up to 10."""
    n = len(ai_native_signals(description))
    if n == 0:
        return min(score, AI_NATIVE_CAP_WITHOUT_SIGNAL)
    if score < AI_NATIVE_MIN_FIT:
        return score
    return min(10, score + min(n, 2))
