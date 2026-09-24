"""Supervised LinkedIn Easy Apply -- Teodor's choice, 2026-09-24 (option B).

Scout never touches his LinkedIn account. Automating Easy Apply would mean
driving his real, logged-in account, which LinkedIn's User Agreement prohibits
and actively detects -- the realistic cost is a restricted account. So for a
LinkedIn job that clears Scout's funnel but leads to no form Scout can fill
(~92% of LinkedIn rows in the first measured run, 2026-09-24), Scout sends HIM
the link, the tailored CV and cover letter, and an answer sheet, and he clicks
Easy Apply himself.

Easy Apply's questions are only visible when logged in, so the sheet cannot be
per-job: it covers the questions Easy Apply commonly asks, from answers.yaml,
and marks anything Scout does not know for him to answer -- never guessed.

A handoff is NOT an application. Only he knows whether he clicked Submit, so no
ledger row is written; recording one would claim an application that may never
happen and block the company through norm_key.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from cv_tailor.scout_queue import queue_root

DEFAULT_DAILY_CAP = 10          # his number, 2026-09-24
_UNKNOWN = "answer yourself"


def daily_cap() -> int:
    """Handoffs per day. Read per call so a config change needs no restart."""
    try:
        return max(0, int(os.environ.get("LINKEDIN_HANDOFF_DAILY_CAP", DEFAULT_DAILY_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def today() -> date:
    """Handoffs are counted per UTC day, matching handed_off_at's timestamps."""
    return datetime.now(timezone.utc).date()


def handoffs_on(day: date, *, queue_dir=None) -> int:
    """How many jobs were handed to him on `day`, across every queue file.

    Counted from the queue itself rather than a separate counter, so it cannot
    drift from what was actually sent. An unreadable day file counts as zero
    rather than raising: a corrupt file must not block every later handoff."""
    root = queue_root(queue_dir)
    if not root.exists():
        return 0
    want, total = day.isoformat(), 0
    for path in root.glob("*/jobs.json"):
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for e in rows if isinstance(rows, list) else []:
            if e.get("status") == "handed_off" and str(e.get("handed_off_at") or "")[:10] == want:
                total += 1
    return total


def _yes_no(value) -> str:
    return _UNKNOWN if value is None else ("Yes" if value else "No")


def answer_sheet(profile: dict, answers: dict) -> str:
    """The common Easy Apply questions, answered from what Scout holds."""
    contact = (profile or {}).get("contact", {}) or {}
    a = answers or {}

    def val(key, fmt=str):
        v = a.get(key)
        return _UNKNOWN if v in (None, "", []) else fmt(v)

    rows = [
        ("Full name", contact.get("name") or _UNKNOWN),
        ("Email", contact.get("email") or _UNKNOWN),
        ("Phone", contact.get("phone") or _UNKNOWN),
        ("Years of experience", val("years_experience")),
        ("Work authorisation", val("work_authorization")),
        ("Languages", val("languages_spoken", lambda v: ", ".join(map(str, v)))),
        ("Notice period", val("notice_period")),
        ("Earliest start", val("start_availability_days",
                               lambda v: f"Within {v} days of an offer")),
        ("Salary expectation", val("salary_fulltime_gross_eur_month",
                                   lambda v: f"{v} EUR gross per month")),
        ("Open to regular travel", _yes_no(a.get("open_to_travel"))),
        ("In-person interview", _yes_no(a.get("in_person_interview"))),
        ("Working-hours overlap", val("timezone_overlap_ok",
                                      lambda v: "Can cover " + ", ".join(map(str, v)))),
        ("Relocation", val("relocation")),
        ("How did you hear", val("how_heard")),
    ]
    return "\n".join(f"- {label}: {value}" for label, value in rows)


def format_card(entry: dict, sheet: str) -> str:
    """The Telegram message for one job. Plain text: no markup to escape."""
    score = entry.get("score")
    lines = [
        f"LinkedIn - apply yourself: {entry.get('title') or '?'}",
        f"{entry.get('company') or '?'}" + (f" | score {score}/10" if score is not None else ""),
    ]
    if entry.get("why"):
        lines.append(f"Why: {entry['why']}")
    lines += [
        f"Link: {entry.get('url') or entry.get('apply_target') or '?'}",
        "",
        "Answer sheet (from your answers.yaml; Easy Apply's own questions are "
        "only visible once you open it):",
        sheet,
        "",
        "Tailored CV and cover letter attached. Scout has NOT recorded this as "
        "applied -- only you know if you submit.",
    ]
    return "\n".join(lines)
