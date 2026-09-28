#!/usr/bin/env python3
"""Adopt the job on the page Teodor is on into his Scout list, tailored.

Scout Fill (2026-09-28): he clicks the Scout icon on an application form for a
job Scout never found. This finds the job description (adopt.find_posting),
adds the job to today's queue as one on his apply-yourself list, and assembles
the package (CV + cover letter tailored to THAT description), so the extension
then fills and uploads exactly as for a job Scout found itself.

stdin:  {"url": str, "title": str?, "text": str?}   (from a web page: untrusted)
stdout: {"date", "id", "company", "title", "tailored": bool, "reused": bool}
exit:   0 ok | 2 bad input | 3 no job description found | 5 tailoring failed
Idempotent: the same posting (by URL) is adopted once and reused after that.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from cv_tailor.adopt import AdoptError, find_posting, job_id_for  # noqa: E402
from cv_tailor.scout_queue import add_entry, queue_root, update_entry  # noqa: E402

LOOKBACK_DAYS = 14


def _existing(job_id: str) -> tuple[str, dict] | None:
    root = queue_root()
    for back in range(LOOKBACK_DAYS):
        day = (date.today() - timedelta(days=back)).isoformat()
        try:
            entries = json.loads((root / day / "jobs.json").read_text())
        except (OSError, ValueError):
            continue
        for e in entries:
            if e.get("id") == job_id:
                return day, e
    return None


def _load_dotenv() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv=None, *, fetcher=None, assembler=None) -> int:
    try:
        payload = json.load(sys.stdin)
        url = str(payload["url"])[:2000]
    except (ValueError, KeyError, TypeError, OSError):
        return 2
    if not url.startswith("https://"):
        return 2
    title = str(payload.get("title") or "")[:500]
    text = str(payload.get("text") or "")[:60_000]
    company = str(payload.get("company") or "")[:200]
    try:
        posting = find_posting(url, title, text, company, **({"fetcher": fetcher} if fetcher else {}))
    except AdoptError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    job_id = job_id_for(posting["url"])

    found = _existing(job_id)
    if found and found[1].get("cv_path"):
        day, e = found
        print(json.dumps({"date": day, "id": job_id, "company": e.get("company"), "title": e.get("title"),
                          "tailored": True, "reused": True}))
        return 0

    day = found[0] if found else date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    entry = add_entry(day, {
        "id": job_id, "source": "extension", "title": posting["title"], "company": posting["company"],
        "location": posting["location"], "url": posting["url"], "score": None,
        "why": "Opened by you in Scout Fill", "matched": [], "apply_options": [], "track": "ai",
        "package_dir": None, "cv_path": None, "cover_letter_path": None,
        "apply_method": "portal", "apply_target": url.split("#")[0], "status": "handed_off",
        "decided_at": now, "email_subject": None, "approved_by": "teodor",
        "handed_off_at": now, "status_changed_at": now,
        "handoff_reason": "Opened by you in Scout Fill",
    }, posting["description"])

    if assembler is None:
        _load_dotenv()
        from cv_tailor.assemble import assemble_package
        assembler = assemble_package
    try:
        meta = assembler(entry, day)
    except Exception as exc:  # noqa: BLE001 -- he still gets generic answers
        print(f"tailoring failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 5

    def _write_back(e: dict) -> None:
        e["package_dir"] = meta["package_dir"]
        e["cv_path"] = meta["cv_path"]
        e["cover_letter_path"] = meta["cover_letter_path"]

    update_entry(day, job_id, _write_back)
    print(json.dumps({"date": day, "id": job_id, "company": posting["company"], "title": posting["title"],
                      "tailored": True, "reused": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
