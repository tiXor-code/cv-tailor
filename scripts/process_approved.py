#!/usr/bin/env python3
"""Process approved candidates from a weekly scan.

Reads scans/<scan-date>.json, looks up the rows you approve by 1-indexed position,
then for each: fetches the full JD via Playwright if needed (already in description),
runs the tailor pipeline (tailor.py), appends a CRM row, and opens the URL in your
default browser.

Usage:
  python scripts/process_approved.py <scan-date> <row-numbers>

Examples:
  python scripts/process_approved.py 2026-05-27 1,3,5
  python scripts/process_approved.py 2026-05-27 4
"""
import argparse
import json
import re
import subprocess
import sys
import unicodedata
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.slug import job_slug


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("scan_date", help="Date of the scan, e.g. 2026-05-27")
    p.add_argument("rows", help="Comma-separated 1-indexed row numbers, e.g. 1,3,5")
    p.add_argument("--no-open", action="store_true",
                   help="Don't open the application URL in the browser (useful for testing).")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    scan_path = ROOT / "scans" / f"{args.scan_date}.json"
    if not scan_path.exists():
        print(f"scan file not found: {scan_path}", file=sys.stderr)
        sys.exit(2)

    items = json.loads(scan_path.read_text())
    if not items:
        print(f"scan {args.scan_date} has zero candidates", file=sys.stderr)
        sys.exit(1)

    try:
        row_indices = [int(x.strip()) for x in args.rows.split(",")]
    except ValueError:
        print(f"could not parse row numbers: {args.rows!r}", file=sys.stderr)
        sys.exit(2)

    for idx in row_indices:
        if idx < 1 or idx > len(items):
            print(f"row {idx} out of range (1..{len(items)})", file=sys.stderr)
            continue
        item = items[idx - 1]
        job = item["job"]
        company = job["org"]
        role = job["title"]
        url = job["url"]
        location = job["location"]

        slug = f"{date.today().isoformat()}-{job_slug(company, role)[11:]}"
        job_dir = ROOT / "jobs" / slug
        job_dir.mkdir(parents=True, exist_ok=True)

        # Synthesize a JD file from the scan description (it's already the full text).
        jd_path = job_dir / "_jd_source.txt"
        jd_path.write_text(
            f"Company: {company}\nJob Title: {role}\nLocation: {location}\nURL: {url}\n\n"
            f"Description:\n{_fetch_description(item)}\n"
        )

        print(f"\n=== [{idx}] {company} / {role} ===")
        print(f"   slug: {slug}")
        print(f"   tailoring CV...")

        result = subprocess.run(
            [sys.executable, "scripts/tailor.py", str(jd_path),
             "--company", company, "--role", role, "--slug", slug],
            cwd=ROOT, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"   tailor failed (exit {result.returncode}):", file=sys.stderr)
            print(result.stderr[-800:], file=sys.stderr)
            continue
        for line in result.stdout.splitlines():
            if line.startswith("PDF:") or line.startswith("Pitch:") or line.startswith("Gaps") or line.startswith("  -"):
                print(f"   {line}")

        crm_result = subprocess.run(
            [sys.executable, "scripts/crm_add.py", str(job_dir / "fields.json")],
            cwd=ROOT, capture_output=True, text=True,
        )
        if crm_result.returncode == 0:
            print(f"   {crm_result.stdout.strip()}")
        else:
            print(f"   crm_add: {crm_result.stderr.strip()[-200:]}", file=sys.stderr)

        if not args.no_open and url:
            subprocess.run(["open", url], check=False)
            print(f"   opened: {url}")


def _fetch_description(item: dict) -> str:
    """For Ashby jobs, the scan stored only metadata; we need to re-fetch description."""
    job = item["job"]
    if job.get("source") == "ashby" and job.get("raw_id"):
        return _fetch_ashby_description(job["url"])
    return ""


def _fetch_ashby_description(job_url: str) -> str:
    """Pull the descriptionHtml from Ashby for a single job URL."""
    import re
    import urllib.request
    from cv_tailor.job_sources import _strip_html

    # job_url like https://jobs.ashbyhq.com/<org>/<id>
    m = re.match(r"https?://jobs\.ashbyhq\.com/([^/]+)/([^/?#]+)", job_url)
    if not m:
        return ""
    org_slug, job_id = m.group(1), m.group(2)
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{org_slug}"
    req = urllib.request.Request(api_url, headers={"Accept": "application/json", "User-Agent": "cv-tailor/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    for j in data.get("jobs", []):
        if j.get("id") == job_id:
            return _strip_html(j.get("descriptionHtml", ""))
    return ""


if __name__ == "__main__":
    main()
