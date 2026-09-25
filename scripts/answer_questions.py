#!/usr/bin/env python3
"""Answer a live form's questions for the one-click fill extension.

Teodor, 2026-09-25: "the crippling anxiety of repeatedly inputting my data".
When a site refuses Scout's own browser (Ashby's reCAPTCHA scoring), he submits
from his real Chrome; the Scout extension fills the form first. It scrapes the
questions it finds and asks this script -- via admin -> mac-sidecar -- for
Scout's answers: the SAME engine the automated adapters use (his saved
question_answers, profile facts, answers.yaml, and answers composed from the
job's cover letter). Anything that cannot be grounded comes back null with
needs_you=true, and the extension highlights it. Nothing here submits anything.

stdin : {"date": "YYYY-MM-DD", "id": "<job id>",
         "questions": [{"label", "kind", "required", "options"}]}
stdout: {"ok": true, "contact": {...}, "cover_letter": "...",
         "answers": [{"label", "value", "source", "needs_you"}]}

The questions come from a web page: untrusted. Kinds are allow-listed and
every string is length-capped before it reaches the answer engine.

Exit codes: 0 ok, 2 bad input, 4 unknown job.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.answers import load_answers  # noqa: E402
from cv_tailor.portal.base import screening_context  # noqa: E402
from cv_tailor.profile import load_profile  # noqa: E402
from cv_tailor.scout_queue import queue_root  # noqa: E402
from cv_tailor.screening import Question, answer_question, consent_is_application_only  # noqa: E402

MAX_QUESTIONS = 60
MAX_LABEL = 500
MAX_OPTIONS = 100
MAX_OPTION = 200
KINDS = {"text", "textarea", "select", "radio", "checkbox", "number", "consent"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    """The sidecar spawns this with launchd's bare environment."""
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def _contact(profile: dict) -> dict:
    c = dict((profile or {}).get("contact") or {})
    parts = str(c.get("name") or "").split()
    c["first_name"] = parts[0] if parts else ""
    c["last_name"] = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {k: str(v) for k, v in c.items() if v}


def _questions(raw) -> list[Question] | None:
    if not isinstance(raw, list):
        return None
    out = []
    for q in raw[:MAX_QUESTIONS]:
        if not isinstance(q, dict) or q.get("kind") not in KINDS:
            return None
        opts = tuple(str(o)[:MAX_OPTION] for o in (q.get("options") or [])[:MAX_OPTIONS])
        out.append(Question(label=str(q.get("label") or "")[:MAX_LABEL], kind=q["kind"],
                            required=bool(q.get("required")), options=opts))
    return out


def main(argv=None, *, client=None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 2
    date, job_id = str(payload.get("date") or ""), str(payload.get("id") or "")
    questions = _questions(payload.get("questions"))
    if not _DATE_RE.match(date) or not _ID_RE.match(job_id) or questions is None:
        return 2
    try:
        entries = json.loads((queue_root() / date / "jobs.json").read_text())
    except (OSError, ValueError):
        return 4
    entry = next((e for e in entries if e.get("id") == job_id), None)
    if entry is None:
        return 4

    profile = load_profile(Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml")), strict=True)
    answers = load_answers()
    context = screening_context(entry)
    if client is None and os.environ.get("SCOUT_ANSWER_LLM", "1") == "1" and "pytest" not in sys.modules:
        _load_dotenv()
        try:
            from cv_tailor.tailor_llm import build_azure_client
            client = build_azure_client()
        except Exception as exc:  # noqa: BLE001 -- deterministic tier still answers
            print(f"llm unavailable ({type(exc).__name__}); deterministic answers only", file=sys.stderr)

    out = []
    for q in questions:
        if q.kind == "consent":
            ok = consent_is_application_only(q.options[0] if q.options else q.label)
            out.append({"label": q.label, "value": "yes" if ok else None,
                        "source": "policy:consent-application-only" if ok else "",
                        "needs_you": not ok and q.required})
            continue
        try:
            ans = answer_question(q, profile, answers, client=client, context=context)
        except Exception as exc:  # noqa: BLE001 -- one bad question must not sink the form
            print(f"answer failed for {q.label[:60]!r}: {type(exc).__name__}", file=sys.stderr)
            ans = None
        value = ans.value if ans is not None and ans.value and ans.grounded_in != "policy:skip" else None
        out.append({"label": q.label, "value": value, "source": ans.grounded_in if value else "",
                    "needs_you": value is None and q.required})

    json.dump({"ok": True, "contact": _contact(profile), "cover_letter": context.get("cover_letter", ""),
               "company": entry.get("company", ""), "title": entry.get("title", ""),
               "answers": out}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
