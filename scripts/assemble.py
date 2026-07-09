#!/usr/bin/env python3
"""Scout Phase 2 -- approve-to-assemble (first slice).

Turn ONE approved queued job into a review package: a tailored CV (PDF) plus a
~150-word cover letter in Teodor's voice. No sending. The reviewer opens the
package, and a later slice wires the approve button + the send step.

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
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.cover_llm import check_cover, cover_letter
from cv_tailor.profile import load_profile
from cv_tailor.render import render_html, render_pdf
from cv_tailor.scout_queue import queue_root, read_description
from cv_tailor.slug import job_slug
from cv_tailor.tailor_llm import build_azure_client, tailor
from cv_tailor.validate import validate


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


def resolve_jd(entry: dict, scan_date: str, queue_dir=None) -> tuple[str, str]:
    """Return (jd_text, source_label). Order: descriptions.json sidecar, an inline
    description on the entry, then a best-effort Ashby re-fetch by URL."""
    jd = read_description(scan_date, entry["id"], queue_dir=queue_dir)
    if jd:
        return jd, "descriptions.json"
    inline = (entry.get("description") or "").strip()
    if inline:
        return inline, "inline"
    if entry.get("source") == "ashby":
        try:
            from process_approved import _fetch_ashby_description  # sibling script
            fetched = (_fetch_ashby_description(entry.get("url", "")) or "").strip()
            if fetched:
                return fetched, "ashby-refetch"
        except Exception as exc:  # noqa: BLE001
            print(f"ashby re-fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return "", "none"


def build_jd_text(entry: dict, jd_body: str) -> str:
    return (
        f"Company: {entry.get('company','')}\n"
        f"Job Title: {entry.get('title','')}\n"
        f"Location: {entry.get('location','')}\n"
        f"URL: {entry.get('url','')}\n\n"
        f"Description:\n{jd_body}\n"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Assemble a review package for one approved job")
    ap.add_argument("scan_date", help="e.g. 2026-07-08")
    ap.add_argument("job_id", help="the queue entry id")
    args = ap.parse_args(argv)

    queue_path, entries = _load_queue(args.scan_date)
    entry = _find(entries, args.job_id)
    company = entry.get("company", "") or "Unknown"
    role = entry.get("title", "") or "Unknown role"

    jd_body, jd_source = resolve_jd(entry, args.scan_date)
    if not jd_body:
        sys.exit(
            f"no JD text for {args.job_id} ({entry.get('source')}). This job predates "
            f"descriptions.json and is not Ashby-refetchable. Re-run the scan to persist "
            f"its description, or add it to descriptions.json manually."
        )
    jd_text = build_jd_text(entry, jd_body)

    profile_path = Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml"))
    templates_dir = Path(os.environ.get("CV_TAILOR_TEMPLATES", ROOT / "templates"))
    profile = load_profile(profile_path, strict=True)

    client = build_azure_client()
    print(f"tailoring CV for {company} / {role} (JD via {jd_source}, {len(jd_body)} chars)...", file=sys.stderr)
    fields = tailor(profile, jd_text, client=client)
    fields.setdefault("job_meta", {})["company"] = company
    fields["job_meta"]["role"] = role

    # canonical-case skills (LLMs lowercase), then honesty/schema validation
    canon = {s.lower(): s for grp in profile.get("skills", {}).values() for s in grp}
    fields["skills_emphasis"] = [canon.get(s.lower(), s) for s in fields.get("skills_emphasis", [])]
    errors = validate(profile, fields)
    if errors:
        sys.exit("CV validation failed (honesty guard):\n  - " + "\n  - ".join(errors))

    slug = job_slug(company, role)
    pkg_dir = queue_root() / args.scan_date / "packages" / slug
    pkg_dir.mkdir(parents=True, exist_ok=True)

    html = render_html(profile, fields, template_dir=templates_dir)
    (pkg_dir / "cv.html").write_text(html)
    cv_pdf = pkg_dir / "cv.pdf"
    render_pdf(html, css_path=templates_dir / "cv.css", out_path=cv_pdf)

    print("writing cover letter...", file=sys.stderr)
    letter = cover_letter(profile, jd_text, fields, client=client)
    cover_warnings = check_cover(letter)
    (pkg_dir / "cover_letter.md").write_text(letter + "\n")

    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "job_id": entry["id"], "company": company, "role": role,
        "url": entry.get("url"), "source": entry.get("source"),
        "apply_method": entry.get("apply_method"), "slug": slug,
        "jd_source": jd_source, "assembled_at": now,
        "one_line_pitch": fields.get("one_line_pitch"),
        "gaps_honest": fields.get("gaps_honest", []),
        "jd_keywords_matched": fields.get("jd_keywords_matched", []),
        "cover_letter_warnings": cover_warnings,
        "cover_letter_words": len(re.findall(r"\b[\w'-]+\b", letter)),
    }
    (pkg_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    # write-back into the queue entry (files are the source of truth)
    entry["package_dir"] = str(pkg_dir)
    entry["cv_path"] = str(cv_pdf)
    entry["cover_letter_path"] = str(pkg_dir / "cover_letter.md")
    entry["status"] = "assembled"
    entry["decided_at"] = now
    queue_path.write_text(json.dumps(entries, indent=2))

    print(f"\n=== assembled: {slug} ===")
    print(f"package:  {pkg_dir}")
    print(f"cv:       {cv_pdf}")
    print(f"cover:    {pkg_dir / 'cover_letter.md'}  ({meta['cover_letter_words']} words)")
    if cover_warnings:
        print("cover-letter warnings:")
        for w in cover_warnings:
            print(f"  - {w}")
    if fields.get("gaps_honest"):
        print("honest gaps: " + "; ".join(fields["gaps_honest"]))
    print(f"queue status -> assembled ({queue_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
