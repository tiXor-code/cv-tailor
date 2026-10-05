#!/usr/bin/env python3
"""Compare a LinkedIn "Save to PDF" export with profile.yaml.

Reads a file he downloaded himself; never contacts LinkedIn (no scraping, no
login) and never writes profile.yaml. Reports email, phone, name, role titles,
companies and month-precision dates that disagree, each with the fix to make.

Usage:
  python scripts/linkedin_sync.py EXPORT.pdf|EXPORT.txt [--json] [--out PATH]
Exit codes: 0 everything agrees, 1 mismatches found, 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.linkedin_sync import compare, parse_export, pdf_to_text, to_markdown  # noqa: E402
from cv_tailor.profile import load_profile  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="LinkedIn export vs profile.yaml")
    ap.add_argument("export")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None, help="also write the report here")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    path = Path(args.export)
    if not path.is_file():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2
    try:
        text = pdf_to_text(path)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"error: could not read the export ({type(exc).__name__}: {exc}); "
              "is pdftotext installed (brew install poppler)?", file=sys.stderr)
        return 2
    profile = load_profile(Path(os.environ.get("CV_TAILOR_PROFILE", ROOT / "profile.yaml")))
    export = parse_export(text)
    result = compare(profile, export)
    out = json.dumps(result, indent=2, ensure_ascii=False, default=list) if args.json \
        else to_markdown(result)
    if args.out:
        Path(args.out).write_text(out)
    print(out)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
