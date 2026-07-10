from datetime import date
from unittest.mock import MagicMock, patch
from cv_tailor.sheets import (
    HEADERS, STATUSES, find_row_by_company_role, build_row_from_fields, crm_mark_applied,
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


def test_crm_mark_applied_updates_existing_row():
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [
        HEADERS,
        ["Acme Corp", "Engineer", "", "", "", "", "Saved", "", ""],
    ]
    with patch("cv_tailor.sheets.get_pipeline_worksheet", return_value=worksheet):
        assert crm_mark_applied("Acme Corp", "Engineer", "https://acme.example/jobs/1") is True

    worksheet.update_cell.assert_any_call(2, 7, "Applied")
    worksheet.update_cell.assert_any_call(2, 6, date.today().isoformat())
    worksheet.append_row.assert_not_called()


def test_crm_mark_applied_appends_minimal_row_when_missing():
    worksheet = MagicMock()
    worksheet.get_all_values.return_value = [HEADERS]
    with patch("cv_tailor.sheets.get_pipeline_worksheet", return_value=worksheet):
        assert crm_mark_applied("New Co", "Role X", "https://new.example/jobs/9") is True

    worksheet.append_row.assert_called_once()
    row = worksheet.append_row.call_args[0][0]
    assert row[0] == "New Co"
    assert row[1] == "Role X"
    assert row[3] == "https://new.example/jobs/9"
    assert row[6] == "Applied"
    worksheet.update_cell.assert_not_called()


def test_crm_mark_applied_never_raises_on_failure():
    with patch("cv_tailor.sheets.get_pipeline_worksheet", side_effect=RuntimeError("no creds")):
        assert crm_mark_applied("Acme Corp", "Engineer", "https://acme.example/jobs/1") is False
