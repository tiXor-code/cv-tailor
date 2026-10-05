#!/usr/bin/env python3
"""Park a job Scout Fill's autopilot could not finish (Scout Fill 0.8, 2026-10-05).

"Run my list" moves on instead of stopping: the job stays on his list with the
reason (login needed, a CAPTCHA, questions only he can answer) so he can
answer everything in one batch. Parked jobs leave the run queue until he
clears them by answering or applying.

stdin:  {"date", "id", "reason", "questions"?: [str]}   (from the extension: untrusted)
exit:   0 parked | 2 bad input | 4 unknown job
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.scout_queue import update_entry  # noqa: E402

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_REASON = 300
MAX_QUESTIONS = 20
MAX_QUESTION = 300


def _clean(text, limit: int) -> str:
    return _CTRL_RE.sub(" ", str(text or "")).strip()[:limit]


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        date, job_id = str(payload["date"]), str(payload["id"])
    except (ValueError, KeyError, TypeError, OSError):
        return 2
    if not _DATE_RE.match(date) or not _ID_RE.match(job_id):
        return 2
    reason = _clean(payload.get("reason"), MAX_REASON)
    if not reason:
        return 2
    raw = payload.get("questions") or []
    questions = [q for q in (_clean(x, MAX_QUESTION) for x in raw[:MAX_QUESTIONS]) if q] if isinstance(raw, list) else []

    def _park(e: dict) -> None:
        e["parked_at"] = datetime.now(timezone.utc).isoformat()
        e["park_reason"] = reason
        e["park_questions"] = questions

    try:
        update_entry(date, job_id, _park)
    except (KeyError, FileNotFoundError, ValueError):
        return 4
    print(json.dumps({"ok": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
