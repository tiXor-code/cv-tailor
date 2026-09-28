#!/usr/bin/env python3
"""Render a package's cover_letter.md as cover_letter.pdf (Scout Fill, 2026-09-28).

Some application forms (join.com) ask for the cover letter as a file upload.
Rendered from the .md every time, so an edit he makes to the letter is what
gets uploaded.

Usage: cover_pdf.py <package_dir>     (must be inside the Scout queue root)
exit:  0 ok | 2 bad dir | 3 no cover_letter.md
"""
from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.profile import load_profile  # noqa: E402
from cv_tailor.render import render_pdf  # noqa: E402
from cv_tailor.scout_queue import queue_root  # noqa: E402


def cover_html(contact: dict, company: str, role: str, letter: str) -> str:
    e = lambda s: html.escape(str(s or ""))  # noqa: E731
    bits = [contact.get(k) for k in ("location", "email", "phone", "website", "linkedin")]
    paragraphs = [p.strip() for p in letter.strip().split("\n\n") if p.strip()]
    body = "\n".join(f"<p class=\"para\">{e(' '.join(p.split()))}</p>" for p in paragraphs)
    heading = f"Application: {e(role)}, {e(company)}" if company or role else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{e(contact.get('name'))} - Cover letter</title>
<style>.para{{margin:0 0 9pt 0;line-height:1.5}} .re{{margin:18pt 0 12pt 0;font-weight:700}} .sign{{margin-top:14pt}}</style>
</head><body>
<header class="contact"><h1>{e(contact.get('name'))}</h1>
<p class="contact-line">{' &middot; '.join(e(b) for b in bits if b)}</p></header>
<p class="re">{heading}</p>
<p class="para">Dear {e(company) + ' team' if company else 'Hiring team'},</p>
{body}
<p class="sign">Kind regards,<br>{e(contact.get('name'))}</p>
</body></html>"""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        return 2
    pkg = Path(argv[0]).resolve()
    root = queue_root().resolve()
    if not pkg.is_dir() or root not in pkg.parents:
        return 2
    md = pkg / "cover_letter.md"
    if not md.is_file():
        return 3
    try:
        meta = json.loads((pkg / "meta.json").read_text())
    except (OSError, ValueError):
        meta = {}
    profile = load_profile(Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml")), strict=False)
    templates = Path(os.environ.get("CV_TAILOR_TEMPLATES", ROOT / "templates"))
    page = cover_html(profile.get("contact") or {}, meta.get("company", ""), meta.get("role", ""), md.read_text())
    render_pdf(page, css_path=templates / "cv.css", out_path=pkg / "cover_letter.pdf")
    print(str(pkg / "cover_letter.pdf"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
