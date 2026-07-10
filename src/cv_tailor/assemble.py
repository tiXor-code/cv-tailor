"""Assemble one approved queued job into a review/send package.

Everything scripts/assemble.py used to do between JD resolution and
meta.json write, extracted into an importable function so both the manual
CLI (scripts/assemble.py) and the post-approval orchestrator
(scripts/apply_approved.py) share one pipeline. This module never writes to
jobs.json -- the caller owns all queue status transitions.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cv_tailor.cover_llm import check_cover, cover_letter
from cv_tailor.profile import load_profile
from cv_tailor.render import render_html, render_pdf
from cv_tailor.scout_queue import queue_root, read_description
from cv_tailor.slug import job_slug
from cv_tailor.tailor_llm import build_azure_client, tailor
from cv_tailor.validate import validate

ROOT = Path(__file__).resolve().parent.parent.parent


class AssembleError(Exception):
    """Raised when a package cannot be assembled: no JD text to tailor from,
    or the tailored fields fail the honesty guard against profile.yaml."""


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


def assemble_package(entry: dict, scan_date: str, *, queue_dir=None, client=None) -> dict:
    """Tailor + render + validate one job into a package dir. Returns the
    meta dict plus package_dir/cv_path/cover_letter_path (str paths). Does
    NOT write anything back into jobs.json -- the caller owns that.

    Raises AssembleError on missing JD text or an honesty-validation failure.
    """
    company = entry.get("company", "") or "Unknown"
    role = entry.get("title", "") or "Unknown role"

    jd_body, jd_source = resolve_jd(entry, scan_date, queue_dir=queue_dir)
    if not jd_body:
        raise AssembleError(
            f"no JD text for {entry.get('id')} ({entry.get('source')}). This job predates "
            f"descriptions.json and is not Ashby-refetchable. Re-run the scan to persist "
            f"its description, or add it to descriptions.json manually."
        )
    jd_text = build_jd_text(entry, jd_body)

    profile_path = Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml"))
    templates_dir = Path(os.environ.get("CV_TAILOR_TEMPLATES", ROOT / "templates"))
    profile = load_profile(profile_path, strict=True)

    client = client or build_azure_client()
    fields = tailor(profile, jd_text, client=client)
    fields.setdefault("job_meta", {})["company"] = company
    fields["job_meta"]["role"] = role

    # canonical-case skills (LLMs lowercase), then drop any skill the LLM
    # invented that isn't in profile.skills at all. Dropping a claimed
    # emphasis is honest -- removing a claim can't fabricate anything -- so
    # unlike an invented experience/project/summary id, it must not dead-end
    # the whole assembly. Record what got dropped for the caller/meta.json.
    canon = {s.lower(): s for grp in profile.get("skills", {}).values() for s in grp}
    emphasis = [canon.get(s.lower(), s) for s in fields.get("skills_emphasis", [])]
    fields["skills_emphasis"] = [s for s in emphasis if s.lower() in canon]
    skills_dropped = [s for s in emphasis if s.lower() not in canon]

    # remaining honesty/schema validation still hard-fails on anything that
    # references content that would otherwise render as fact (experience,
    # project, and summary ids).
    errors = validate(profile, fields)
    if errors:
        raise AssembleError("CV validation failed (honesty guard):\n  - " + "\n  - ".join(errors))

    slug = job_slug(company, role)
    pkg_dir = queue_root(queue_dir) / scan_date / "packages" / slug
    pkg_dir.mkdir(parents=True, exist_ok=True)

    html = render_html(profile, fields, template_dir=templates_dir)
    (pkg_dir / "cv.html").write_text(html)
    cv_pdf = pkg_dir / "cv.pdf"
    render_pdf(html, css_path=templates_dir / "cv.css", out_path=cv_pdf)

    letter = cover_letter(profile, jd_text, fields, client=client)
    cover_warnings = check_cover(letter)
    cover_letter_path = pkg_dir / "cover_letter.md"
    cover_letter_path.write_text(letter + "\n")

    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "job_id": entry.get("id"), "company": company, "role": role,
        "url": entry.get("url"), "source": entry.get("source"),
        "apply_method": entry.get("apply_method"), "slug": slug,
        "jd_source": jd_source, "assembled_at": now,
        "one_line_pitch": fields.get("one_line_pitch"),
        "gaps_honest": fields.get("gaps_honest", []),
        "jd_keywords_matched": fields.get("jd_keywords_matched", []),
        "skills_dropped": skills_dropped,
        "cover_letter_warnings": cover_warnings,
        "cover_letter_words": len(re.findall(r"\b[\w'-]+\b", letter)),
    }
    (pkg_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    result = dict(meta)
    result["package_dir"] = str(pkg_dir)
    result["cv_path"] = str(cv_pdf)
    result["cover_letter_path"] = str(cover_letter_path)
    return result
