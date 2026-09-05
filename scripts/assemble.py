#!/usr/bin/env python3
"""Scout Phase 2 -- approve-to-assemble (manual CLI).

Turn ONE approved queued job into a review package: a tailored CV (PDF) plus a
~150-word cover letter in Teodor's voice. No sending. Thin wrapper over
cv_tailor.assemble.assemble_package (the shared pipeline the apply_approved.py
orchestrator also uses); this script's own job is CLI I/O and the queue
write-back.

Usage:
  python scripts/assemble.py <scan-date> <job-id>
  python scripts/assemble.py 2026-07-08 c0fdf825acf477c8

Reads   ~/clawd/var/scout/<date>/jobs.json      (find entry by id)
        ~/clawd/var/scout/<date>/descriptions.json  (full JD, written at scan time)
Writes  ~/clawd/var/scout/<date>/packages/<slug>/{cv.pdf, cv.html, cover_letter.md, meta.json}
        and writes package_dir/cv_path/cover_letter_path/status back into jobs.json.

Env:
  SCOUT_QUEUE_DIR   override the queue root (used by tests; never touches prod state)
  CV_TAILOR_PROFILE / CV_TAILOR_TEMPLATES   override profile.yaml / templates dir
Run under the cv-tailor venv (weasyprint + gspread etc.); system python3 lacks deps.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.assemble import AssembleError, assemble_package
from cv_tailor.scout_queue import queue_root, update_entry
from cv_tailor.tailor_llm import build_azure_client


def _load_queue(scan_date: str, queue_dir=None) -> tuple[Path, list]:
    path = queue_root(queue_dir) / scan_date / "jobs.json"
    if not path.exists():
        sys.exit(f"queue not found: {path}")
    return path, json.loads(path.read_text())


def _find(entries: list, job_id: str) -> dict:
    for e in entries:
        if e.get("id") == job_id:
            return e
    ids = ", ".join(e.get("id", "?") for e in entries) or "(empty)"
    sys.exit(f"job id {job_id!r} not in queue. Available: {ids}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble a review package for one approved job")
    ap.add_argument("scan_date", help="e.g. 2026-07-08")
    ap.add_argument("job_id", help="the queue entry id")
    args = ap.parse_args(argv)

    queue_path, entries = _load_queue(args.scan_date)
    entry = _find(entries, args.job_id)
    company = entry.get("company", "") or "Unknown"
    role = entry.get("title", "") or "Unknown role"

    print(f"tailoring CV for {company} / {role}...", file=sys.stderr)
    client = build_azure_client()
    try:
        meta = assemble_package(entry, args.scan_date, client=client)
    except AssembleError as exc:
        sys.exit(str(exc))

    now = datetime.now(timezone.utc).isoformat()

    def _write_back(e: dict) -> None:
        e["package_dir"] = meta["package_dir"]
        e["cv_path"] = meta["cv_path"]
        e["cover_letter_path"] = meta["cover_letter_path"]
        e["status"] = "assembled"
        e["decided_at"] = now

    update_entry(args.scan_date, args.job_id, _write_back)

    pkg_dir = Path(meta["package_dir"])
    print(f"\n=== assembled: {meta['slug']} (JD via {meta['jd_source']}) ===")
    print(f"package:  {pkg_dir}")
    print(f"cv:       {meta['cv_path']}")
    print(f"cover:    {meta['cover_letter_path']}  ({meta['cover_letter_words']} words)")
    if meta.get("cover_letter_warnings"):
        print("cover-letter warnings:")
        for w in meta["cover_letter_warnings"]:
            print(f"  - {w}")
    if meta.get("gaps_honest"):
        print("honest gaps: " + "; ".join(meta["gaps_honest"]))
    print(f"queue status -> assembled ({queue_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
