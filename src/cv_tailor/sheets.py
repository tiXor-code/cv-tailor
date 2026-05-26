"""Google Sheets CRM helpers.

The service-account JSON path and the target Sheet ID come from env.
Workbook layout: one tab named 'Pipeline' with the columns in HEADERS.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Any

HEADERS = [
    "Company", "Role", "Location", "JD link", "CV file",
    "Date applied", "Status", "Next action", "Notes",
]

STATUSES = [
    "Saved", "Applied", "Reply", "Interview", "Offer", "Rejected", "Ghosted",
]

PIPELINE_TAB = "Pipeline"


def build_row_from_fields(fields: dict, cv_path: str) -> list[str]:
    meta = fields.get("job_meta", {})
    return [
        meta.get("company", ""),
        meta.get("role", ""),
        meta.get("location") or "",
        meta.get("jd_url") or "",
        f"file://{cv_path}",
        "",            # Date applied (blank until applied)
        "Saved",       # Status
        "Apply",       # Next action
        "",            # Notes
    ]


def find_row_by_company_role(worksheet, company: str, role: str) -> int | None:
    """Return 1-indexed row number if found, else None. Case-insensitive."""
    values = worksheet.get_all_values()
    target = (company.strip().lower(), role.strip().lower())
    for i, row in enumerate(values):
        if i == 0:
            continue  # header
        if len(row) < 2:
            continue
        if (row[0].strip().lower(), row[1].strip().lower()) == target:
            return i + 1  # gspread is 1-indexed
    return None


def get_pipeline_worksheet(sa_path: Path | str | None = None, sheet_id: str | None = None):
    """Lazy import gspread + open the Pipeline worksheet.

    Auth resolution: if a non-empty service-account JSON exists at sa_path
    (or GOOGLE_SERVICE_ACCOUNT_PATH), use it. Otherwise fall back to
    Application Default Credentials (set up via
    `gcloud auth application-default login --scopes=...spreadsheets,drive`).
    """
    import gspread

    sa_path = sa_path or os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH")
    sheet_id = sheet_id or os.environ["SHEET_ID"]

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if sa_path and Path(sa_path).exists() and Path(sa_path).stat().st_size > 0:
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(str(sa_path), scopes=scopes)
    else:
        from google.auth import default
        creds, _ = default(scopes=scopes)

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(sheet_id)
    try:
        return spreadsheet.worksheet(PIPELINE_TAB)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(PIPELINE_TAB, rows=1000, cols=len(HEADERS))


def update_status(worksheet, company: str, role: str, status: str, date_applied: str | None = None):
    """Update Status (col G) and optionally Date applied (col F) for the matching row."""
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r} (valid: {STATUSES})")
    row = find_row_by_company_role(worksheet, company, role)
    if row is None:
        raise LookupError(f"no row for company={company!r} role={role!r}")
    worksheet.update_cell(row, 7, status)
    if date_applied is not None:
        worksheet.update_cell(row, 6, date_applied)
    return row
