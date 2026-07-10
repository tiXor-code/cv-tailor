"""Grounded answerer for job-application screening questions.

This module decides what gets typed into an employer's application form, so
groundedness is the entire point: nothing it returns may be invented.

Two tiers, deterministic first:

1. Deterministic tier (no LLM). EEO/demographic questions are policy-driven
   (decline option, or skip/None). Everything else is matched by label
   keyword to either `profile.contact` (name/email/phone/location/links) or
   `answers.yaml` (salary/rate/notice/authorization/availability/relocation).
2. LLM tier (gpt-4o-mini, mocked in tests) for anything the deterministic
   tier does not recognize. The model sees ONLY the profile + answers YAML
   and must reply `UNKNOWN` when the question is not grounded in them.
   Select/radio/checkbox answers are matched back to one of the question's
   options (case-insensitive exact match); no match collapses to UNKNOWN.

`answer_question(...)` returns `None` when a REQUIRED question has no
grounded answer -- the caller (a portal adapter) must abort to
`needs_human` rather than submit a guess. An OPTIONAL question with no
grounded answer returns `Answer("", "policy:skip")` instead, so the caller
can leave the field blank without treating it as a failure.
"""
from __future__ import annotations

import os
import re
from typing import Any, NamedTuple

import yaml


class Question(NamedTuple):
    label: str
    kind: str                  # "text" | "textarea" | "select" | "radio" | "checkbox" | "number"
    required: bool
    options: tuple[str, ...] = ()   # for select/radio/checkbox, else ()


class Answer(NamedTuple):
    value: str                 # text to type or the EXACT option string to pick
    grounded_in: str           # "profile:<path>" | "answers:<key>" | "policy:eeo-decline" |
                                # "policy:skip" | "llm:grounded"


# ---------------------------------------------------------------------------
# EEO / demographic policy tier
# ---------------------------------------------------------------------------

# Substring triggers for demographic/EEO-style questions. Checked first and
# handled purely by policy -- never sent to the LLM, never inferred.
_EEO_KEYWORDS = ("gender", "race", "ethnic", "veteran", "disab", "sexual", "pronoun")

# Substrings that mark an option as a "prefer not to answer" style decline.
_DECLINE_PATTERNS = (
    "prefer not",
    "decline to",
    "don't wish",
    "do not wish",
    "not to disclose",
    "not disclose",
    "rather not",
    "choose not to",
)


def _is_eeo_label(label: str) -> bool:
    low = label.lower()
    return any(k in low for k in _EEO_KEYWORDS)


def _find_decline_option(options: tuple[str, ...]) -> str | None:
    for opt in options:
        low = opt.lower()
        if any(p in low for p in _DECLINE_PATTERNS):
            return opt
    return None


def _eeo_answer(q: Question) -> Answer | None:
    """Return the decline answer, a skip, or None (required, no decline option)."""
    decline = _find_decline_option(q.options)
    if decline is not None:
        return Answer(decline, "policy:eeo-decline")
    if q.required:
        return None
    return Answer("", "policy:skip")


# ---------------------------------------------------------------------------
# Deterministic tier: answers.yaml keyword matching
# ---------------------------------------------------------------------------

def _from_answers(answers: dict, key: str) -> Answer | None:
    value = (answers or {}).get(key)
    if value is None or value == "":
        return None
    return Answer(str(value), f"answers:{key}")


def _from_contact(contact: dict, key: str) -> Answer | None:
    value = (contact or {}).get(key)
    if value is None or value == "":
        return None
    return Answer(str(value), f"profile:contact.{key}")


def _deterministic_answer(q: Question, profile: dict, answers: dict) -> Answer | None:
    label = q.label.lower()
    contact = (profile or {}).get("contact", {}) or {}

    # answers.yaml categories -- checked before generic contact fields so
    # e.g. "relocation" (which contains "location") is never mistaken for
    # the contact location field.
    if "reloc" in label:
        return _from_answers(answers, "relocation")
    if "hourly" in label or ("rate" in label and "hour" in label):
        return _from_answers(answers, "hourly_rate_min_eur")
    if any(k in label for k in ("salary", "compensation", "pay expectation", "expected pay")):
        key = "salary_fulltime_net_eur_month" if "net" in label else "salary_fulltime_gross_eur_month"
        return _from_answers(answers, key)
    if "notice" in label:
        return _from_answers(answers, "notice_period")
    if any(k in label for k in ("visa", "sponsor", "authoriz", "legally work", "eligible to work", "work permit")):
        return _from_answers(answers, "work_authorization")
    if "availab" in label:
        return _from_answers(answers, "availability_parttime")

    # profile.contact fields
    if re.search(r"\bemail\b", label):
        return _from_contact(contact, "email")
    if re.search(r"\bphone\b", label) or "telephone" in label or "mobile number" in label:
        return _from_contact(contact, "phone")
    if "linkedin" in label:
        return _from_contact(contact, "linkedin")
    if "github" in label:
        return _from_contact(contact, "github")
    if "website" in label or "portfolio" in label:
        return _from_contact(contact, "website")
    if "location" in label or "city" in label or "based" in label:
        return _from_contact(contact, "location")
    if re.search(r"\bname\b", label):
        return _from_contact(contact, "name")

    return None


# ---------------------------------------------------------------------------
# LLM tier
# ---------------------------------------------------------------------------

LLM_SYSTEM_PROMPT = """You answer a single job-application screening question for \
Teodor-Cristian Lutoiu, using ONLY the candidate profile and screening-answers YAML \
provided below. Never invent facts.

RULES:
- Answer using ONLY information present in the profile or answers YAML. Do not guess,
  infer beyond what is stated, or invent numbers, dates, employers, or facts.
- If the question cannot be answered from the given information, reply with exactly:
  UNKNOWN
- If the question lists Options, reply with the EXACT text of ONE option (copied
  verbatim), or UNKNOWN if none of them fit.
- Otherwise reply with a short, direct answer (a few words to one sentence). No
  explanations, no markdown, no surrounding quotes, no extra commentary.
- Years-of-experience style questions: answer only if derivable from dates/durations
  actually present in the profile. Do not estimate or round up.
- Work authorization / visa questions: answer strictly from the answers.work_authorization
  text; do not add or infer anything beyond what it says.

Reply with the answer text ONLY (or UNKNOWN). Nothing else."""


def build_llm_messages(q: Question, profile: dict, answers: dict) -> list[dict]:
    profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
    answers_yaml = yaml.safe_dump(answers, sort_keys=False, allow_unicode=True)
    options_block = ""
    if q.options:
        options_block = "\nOptions (reply with the EXACT text of one, or UNKNOWN):\n" + "\n".join(
            f"- {o}" for o in q.options
        )
    user = (
        f"# Candidate profile (profile.yaml)\n```yaml\n{profile_yaml}```\n\n"
        f"# Screening answers (answers.yaml)\n```yaml\n{answers_yaml}```\n\n"
        f"# Question\nLabel: {q.label}\nType: {q.kind}\nRequired: {q.required}{options_block}"
    )
    return [
        {"role": "system", "content": LLM_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _match_option(raw: str, options: tuple[str, ...]) -> str | None:
    """Case-insensitive exact match of the LLM's reply to one of the options."""
    low = raw.strip().lower()
    for opt in options:
        if opt.strip().lower() == low:
            return opt
    return None


def _llm_answer(
    q: Question,
    profile: dict,
    answers: dict,
    *,
    client: Any,
    deployment: str | None = None,
) -> Answer | None:
    deployment = deployment or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    response = client.chat.completions.create(
        model=deployment,
        messages=build_llm_messages(q, profile, answers),
        temperature=0.0,
    )
    raw = (response.choices[0].message.content or "").strip()

    if q.options:
        matched = _match_option(raw, q.options)
        raw = matched if matched is not None else "UNKNOWN"

    if not raw or raw.strip().upper() == "UNKNOWN":
        return None if q.required else Answer("", "policy:skip")

    return Answer(raw, "llm:grounded")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def answer_question(
    q: Question,
    profile: dict,
    answers: dict,
    *,
    client: Any = None,
    deployment: str | None = None,
) -> Answer | None:
    """Answer one screening question, deterministic tier first.

    Returns None when the question is REQUIRED and no grounded answer
    exists (caller must abort to needs_human). Returns Answer("",
    "policy:skip") when the question is OPTIONAL and unanswerable.

    client=None means: deterministic tier only. Anything that would need
    the LLM returns None (adapters may run without a client in tests).
    """
    if _is_eeo_label(q.label):
        return _eeo_answer(q)

    det = _deterministic_answer(q, profile, answers)
    if det is not None:
        return det

    if client is None:
        return None

    return _llm_answer(q, profile, answers, client=client, deployment=deployment)
