"""ATS sanity checks: extract text from PDF and verify recruiter-parser shape."""
import re
import subprocess
from pathlib import Path

REQUIRED_SECTIONS = ["Summary", "Experience", "Skills", "Education"]
SPACED_LETTERS_RE = re.compile(r"\b(?:[A-Za-z]\s){3,}[A-Za-z]\b")


def extract_text(pdf_path: Path | str) -> str:
    """Run pdftotext -layout. Raises CalledProcessError if pdftotext missing."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout


def run_checks(
    text: str,
    profile: dict,
    fields: dict,
    *,
    experiences_by_id: dict,
    projects_by_id: dict,
) -> list[str]:
    warnings: list[str] = []
    lines = text.splitlines()
    head = "\n".join(lines[:5])  # first 5 lines = "first text block"

    # 1. Required sections present, in order.
    last_pos = -1
    for section in REQUIRED_SECTIONS:
        pos = text.find(section)
        if pos < 0:
            warnings.append(f"missing required section: '{section}'")
        elif pos < last_pos:
            warnings.append(f"section '{section}' appears out of order")
        else:
            last_pos = pos

    # 2. Email in head.
    email = profile.get("contact", {}).get("email", "")
    if email and email not in head:
        warnings.append(f"email '{email}' not found in first 5 lines (head was: {head!r})")

    # 3. Phone in head — strip non-digits for tolerant compare.
    phone = profile.get("contact", {}).get("phone", "")
    phone_digits = re.sub(r"\D", "", phone)
    head_digits = re.sub(r"\D", "", head)
    if phone_digits and phone_digits not in head_digits:
        warnings.append(f"phone '{phone}' not found in first 5 lines")

    # 4. No spaced-letter mangling.
    if SPACED_LETTERS_RE.search(text):
        warnings.append("possible spaced-letter mangling detected (e.g. 'T e o d o r')")

    # 5. Each chosen experience appears by company name.
    for exp_id in fields.get("experience_ids_ordered", []):
        company = experiences_by_id.get(exp_id, {}).get("company")
        if company and company not in text:
            warnings.append(f"chosen experience '{exp_id}' (company '{company}') missing from rendered PDF")

    # 6. Each chosen project appears by name.
    for pid in fields.get("project_ids", []):
        name = projects_by_id.get(pid, {}).get("name")
        if name and name not in text:
            warnings.append(f"chosen project '{pid}' (name '{name}') missing from rendered PDF")

    return warnings
