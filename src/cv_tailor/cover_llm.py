"""Cover-letter generation: one Azure OpenAI call that turns (profile, JD,
tailored fields) into a ~150-word note in Teodor's voice.

Same honesty guard as tailor_llm (only facts present in profile.yaml) plus an
anti-slop pass: banned AI-cliche phrases, no em dashes, and a length window. The
generator self-checks and retries once with the warnings fed back in.
"""
from __future__ import annotations

import os
import re
from typing import Any

import yaml

# Phrases that read as AI slop / corporate filler. Teodor's style rules.
BANNED_PHRASES = [
    "i am excited", "i'm excited", "i am thrilled", "i am writing to express",
    "i am writing to apply", "delve", "leverage", "passionate about", "passionate",
    "perfect fit", "perfect candidate", "hit the ground running", "wealth of experience",
    "proven track record", "dynamic", "synergy", "cutting-edge", "game-changer",
    "i believe i would be", "look no further", "as a highly", "results-driven",
    "detail-oriented", "team player", "think outside the box", "circle back",
    # closers and filler that read as generated
    "i am eager", "i'm eager", "eager to contribute", "thank you for considering",
    "showcasing", "showcases my", "demonstrates my capability", "innovative solutions",
    "i would love the opportunity", "align with your", "contribute to your mission",
]
WORDS_MIN, WORDS_MAX = 110, 200

SYSTEM_PROMPT = """You write a short cover-letter note as Teodor-Cristian Lutoiu, applying to one job.

You get his canonical profile (YAML), the job description, and the machine-selected
fields that tailored his CV (pitch, matched keywords, chosen experiences/projects).

HARD RULES:
- Use ONLY facts present in the profile. Never invent employers, titles, dates,
  metrics, or skills. If the JD wants something he lacks, do not claim it.
- LENGTH: write between 130 and 170 words. Count them before you answer. A letter
  under 130 words will be rejected.
- Shape: one tight opener, 2-3 sentences of specific evidence tied to THIS job,
  one close. No PS, no header, no address block, no signature line.
- Voice: direct, human, concrete. First person. Plain verbs.
- BANNED (never write these, in any form): "I am excited/thrilled/eager",
  "I am writing to express", "leverage", "delve", "passionate", "proven track
  record", "perfect fit", "hit the ground running", "results-driven",
  "detail-oriented", "team player", "dynamic", "synergy", "cutting-edge",
  "showcasing", "thank you for considering", "eager to contribute", and similar
  filler. Say what he built, not that he is able to build.
- No em dashes. Use commas or full stops.
- Lead with the single most relevant concrete thing he has built or shipped for
  this role, not with wanting the job. Close on what he wants to do there, plainly.

Return ONLY the letter body text. No JSON, no markdown, no labels."""

REVISE_PROMPT = """Revise the draft below. Keep every sentence that is already concrete and
specific; change ONLY what is needed to fix the listed problems. Do not start over, do not
drop the good evidence, and do not shorten the letter to fix wording. Return ONLY the revised
letter body."""


def _evidence(fields: dict) -> str:
    jm = fields.get("job_meta", {}) or {}
    parts = [
        f"Applying for: {jm.get('role','')} at {jm.get('company','')}".strip(),
        # The pitch comes from the CV step and often carries cliches ("passionate",
        # "proven track record"). Pass it as signal, never as wording to copy.
        f"One-line pitch (signal only, DO NOT reuse its wording): {fields.get('one_line_pitch','')}",
        f"Matched JD keywords: {', '.join(fields.get('jd_keywords_matched', []) or [])}",
        f"Emphasised skills: {', '.join(fields.get('skills_emphasis', []) or [])}",
        f"Chosen experience ids: {', '.join(fields.get('experience_ids_ordered', []) or [])}",
        f"Chosen project ids: {', '.join(fields.get('project_ids', []) or [])}",
    ]
    gaps = fields.get("gaps_honest") or []
    if gaps:
        parts.append("Honest gaps (do NOT paper over these): " + "; ".join(gaps))
    return "\n".join(parts)


def build_messages(
    profile: dict,
    jd_text: str,
    fields: dict,
    extra: str = "",
    previous: str = "",
) -> list[dict]:
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    user = (
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n"
        f"# Job description\n```\n{jd_text}\n```\n\n"
        f"# Tailored fields (evidence to draw from)\n{_evidence(fields)}\n"
    )
    if extra:
        # Revision turn: show the model its own draft so it edits rather than regenerates.
        user += f"\n# {REVISE_PROMPT}\n\n# Problems to fix\n{extra}\n"
        if previous:
            user += f"\n# Your previous draft\n```\n{previous}\n```\n"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def check_cover(text: str) -> list[str]:
    """Anti-slop + shape warnings. Empty list == clean."""
    warns: list[str] = []
    low = (text or "").lower()
    for p in BANNED_PHRASES:
        if p in low:
            warns.append(f"banned phrase: {p!r}")
    if "—" in text or "–" in text:
        warns.append("contains an em/en dash")
    n = len(re.findall(r"\b[\w'-]+\b", text))
    if n < WORDS_MIN:
        warns.append(f"too short ({n} words, min {WORDS_MIN})")
    if n > WORDS_MAX:
        warns.append(f"too long ({n} words, max {WORDS_MAX})")
    return warns


MAX_ATTEMPTS = 3


def cover_letter(
    profile: dict,
    jd_text: str,
    fields: dict,
    *,
    client: Any,
    deployment: str | None = None,
) -> str:
    """Generate the letter, self-check, and revise (up to MAX_ATTEMPTS) with the
    warnings AND the previous draft fed back. Returns the first clean draft, else
    the draft with the fewest warnings; the caller records residual warnings."""
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    extra = ""
    previous = ""
    best: tuple[int, str] | None = None
    for _ in range(MAX_ATTEMPTS):
        resp = client.chat.completions.create(
            model=deployment,
            messages=build_messages(profile, jd_text, fields, extra, previous),
            temperature=0.4,
        )
        text = (resp.choices[0].message.content or "").strip()
        warns = check_cover(text)
        if not warns:
            return text
        if best is None or len(warns) < best[0]:
            best = (len(warns), text)
        previous = text
        extra = "\n".join(f"- {w}" for w in warns)
    return best[1] if best else ""
