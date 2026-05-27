from unittest.mock import MagicMock
from cv_tailor.sheets import (
    HEADERS, STATUSES, find_row_by_company_role, build_row_from_fields,
)

def test_headers_match_spec():
    assert HEADERS == [
        "Company", "Role", "Location", "JD link", "CV file",
        "Date applied", "Status", "Next action", "Notes",
    ]

def test_statuses_match_spec():
    assert STATUSES == [
        "Saved", "Applied", "Reply", "Interview", "Offer", "Rejected", "Ghosted", "Skipped",
    ]

def test_build_row_from_fields_uses_job_meta_and_cv_path():
    fields = {
        "job_meta": {
            "company": "Acme",
            "role": "Engineer",
            "location": "Remote",
            "jd_url": "https://acme.example/jobs/1",
        }
    }
    row = build_row_from_fields(fields, cv_path="/abs/path/cv.pdf")
    assert row[0] == "Acme"
    assert row[1] == "Engineer"
    assert row[2] == "Remote"
    assert row[3] == "https://acme.example/jobs/1"
    assert row[4] == "file:///abs/path/cv.pdf"
    assert row[5] == ""           # Date applied blank
    assert row[6] == "Saved"      # Status
    assert row[7] == "Apply"      # Next action
    assert row[8] == ""           # Notes

def test_find_row_by_company_role_case_insensitive():
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [
        HEADERS,
        ["acme corp", "engineer", "", "", "", "", "Saved", "", ""],
        ["Other", "PM", "", "", "", "", "Applied", "", ""],
    ]
    assert find_row_by_company_role(worksheet, "Acme Corp", "Engineer") == 2
    assert find_row_by_company_role(worksheet, "Missing", "X") is None
