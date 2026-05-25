#!/usr/bin/env python3
"""Tailor a CV for one job description.

Usage:
  python scripts/tailor.py <jd-file> [--company STR] [--role STR] [--slug STR]

Env (optional overrides for tests):
  CV_TAILOR_PROFILE     path to profile.yaml (default: ./profile.yaml)
  CV_TAILOR_TEMPLATES   templates dir (default: ./templates)
  CV_TAILOR_JOBS_DIR    parent dir for job folders (default: ./jobs)
"""
import argparse
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.profile import load_profile
from cv_tailor.tailor_llm import tailor, build_azure_client
from cv_tailor.validate import validate
from cv_tailor.render import render_html, render_pdf
from cv_tailor.ats_check import extract_text, run_checks
from cv_tailor.slug import job_slug


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("jd_path", help="Path to a job description text file.")
    p.add_argument("--company", help="Override company name for slug + CRM.")
    p.add_argument("--role", help="Override role for slug + CRM.")
    p.add_argument("--slug", help="Override entire slug (skip auto-derivation).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    profile_path = Path(os.environ.get("CV_TAILOR_PROFILE", "profile.yaml"))
    templates_dir = Path(os.environ.get("CV_TAILOR_TEMPLATES", "templates"))
    jobs_root = Path(os.environ.get("CV_TAILOR_JOBS_DIR", "jobs"))

    profile = load_profile(profile_path, strict=True)
    jd_text = Path(args.jd_path).read_text(encoding="utf-8")

    client = build_azure_client()
    fields = tailor(profile, jd_text, client=client)

    if args.company:
        fields.setdefault("job_meta", {})["company"] = args.company
    if args.role:
        fields.setdefault("job_meta", {})["role"] = args.role

    errors = validate(profile, fields)
    if errors:
        invalid_path = Path(args.jd_path).parent / "fields.invalid.json"
        invalid_path.write_text(json.dumps(fields, indent=2))
        print("VALIDATION FAILED:\n  - " + "\n  - ".join(errors), file=sys.stderr)
        print(f"Raw response saved to {invalid_path}", file=sys.stderr)
        sys.exit(2)

    if args.slug:
        slug = args.slug
    else:
        slug = job_slug(fields["job_meta"]["company"], fields["job_meta"]["role"])
    job_dir = jobs_root / slug
    job_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(args.jd_path, job_dir / "jd.txt")
    (job_dir / "fields.json").write_text(json.dumps(fields, indent=2))

    html = render_html(profile, fields, template_dir=templates_dir)
    (job_dir / "cv.html").write_text(html)

    pdf_path = job_dir / "cv.pdf"
    render_pdf(html, css_path=templates_dir / "cv.css", out_path=pdf_path)

    cv_text = extract_text(pdf_path)
    (job_dir / "cv.txt").write_text(cv_text)
    warnings = run_checks(
        cv_text, profile, fields,
        experiences_by_id={e["id"]: e for e in profile.get("experiences", [])},
        projects_by_id={p["id"]: p for p in profile.get("projects", [])},
    )

    print(f"\n=== {slug} ===")
    print(f"PDF:     {pdf_path}")
    print(f"Pitch:   {fields.get('one_line_pitch')}")
    if fields.get("gaps_honest"):
        print("Gaps honest:")
        for g in fields["gaps_honest"]:
            print(f"  - {g}")
    if warnings:
        print("ATS warnings:")
        for w in warnings:
            print(f"  - {w}")
        result = {"job_dir": str(job_dir), "warnings": warnings}
        sys.stdout.flush()
        if argv is None:
            sys.exit(1)
        return result

    return {"job_dir": str(job_dir), "warnings": []}


if __name__ == "__main__":
    main()
