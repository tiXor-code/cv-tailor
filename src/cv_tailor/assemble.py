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


def _select_track(profile: dict, entry: dict) -> tuple[str, dict | None]:
    """Resolve the entry's track id and its `tracks:` config block.

    A missing `track` key defaults to "ai". An id that isn't a key in
    profile["tracks"] (a typo, or an id from a newer schema this profile
    predates) also falls back to "ai" -- per the brief, unknown/missing
    track resolves to the ai track's config. If profile.yaml has no
    `tracks:` block at all (older profile, or the ai id isn't configured
    either), the config is None and callers fall back to current
    (unrestricted) behavior -- this never crashes on legacy entries.
    """
    tracks_cfg = profile.get("tracks") or {}
    track = entry.get("track") or "ai"
    cfg = tracks_cfg.get(track)
    if cfg is None and track != "ai":
        track = "ai"
        cfg = tracks_cfg.get("ai")
    return track, cfg


def _profile_for_tailor(profile: dict, track_cfg: dict | None) -> dict:
    """Constrain the summary_pool the tailor LLM sees to exactly the track's
    summary_id, so chosen_summary_id/summary_rewrite are grounded in the
    right background (ai vs content) instead of the LLM picking freely from
    the whole pool. No track config (or the configured summary_id isn't in
    the pool) -> return profile unchanged, the pre-track behavior."""
    if not track_cfg:
        return profile
    summary_id = track_cfg.get("summary_id")
    matched = [s for s in profile.get("summary_pool", []) if s.get("id") == summary_id]
    if not matched:
        return profile
    constrained = dict(profile)
    constrained["summary_pool"] = matched
    return constrained


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

    track, track_cfg = _select_track(profile, entry)
    tailor_profile = _profile_for_tailor(profile, track_cfg)

    client = client or build_azure_client()
    fields = tailor(tailor_profile, jd_text, client=client)
    fields.setdefault("job_meta", {})["company"] = company
    fields["job_meta"]["role"] = role

    # Restrict which skill groups the CV template renders/orders to the
    # track's list (templates/cv.html.j2's optional fields.skills_groups).
    # No track config -> leave fields alone, every profile.skills group
    # renders (current/pre-track behavior).
    if track_cfg and track_cfg.get("skill_groups"):
        fields["skills_groups"] = list(track_cfg["skill_groups"])

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
        "jd_source": jd_source, "assembled_at": now, "track": track,
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
