#!/usr/bin/env python3
"""Append one row to the Pipeline tab for the given fields.json.

Usage:
  python scripts/crm_add.py <fields.json> [--force]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.sheets import (
    build_row_from_fields, find_row_by_company_role, get_pipeline_worksheet,
)


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("fields_path", help="Path to fields.json (cv.pdf is expected in the same dir).")
    p.add_argument("--force", action="store_true",
                   help="Append even if a row with the same Company+Role already exists.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    fields_path = Path(args.fields_path)
    fields = json.loads(fields_path.read_text())
    cv_path = (fields_path.parent / "cv.pdf").resolve()
    if not cv_path.exists():
        print(f"warning: {cv_path} does not exist yet", file=sys.stderr)

    company = fields.get("job_meta", {}).get("company", "")
    role = fields.get("job_meta", {}).get("role", "")
    if not company or not role:
        print("fields.json missing job_meta.company or job_meta.role", file=sys.stderr)
        sys.exit(2)

    ws = get_pipeline_worksheet()

    if not args.force:
        existing = find_row_by_company_role(ws, company, role)
        if existing is not None:
            print(
                f"row for {company!r} / {role!r} already exists at row {existing}. "
                f"Pass --force to add anyway.",
                file=sys.stderr,
            )
            sys.exit(3)

    row = build_row_from_fields(fields, cv_path=str(cv_path))
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"appended row: {company} / {role}")


if __name__ == "__main__":
    main()
