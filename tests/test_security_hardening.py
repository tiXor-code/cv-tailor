"""Regression tests for Aegis issue #9: attacker-controlled URLs and text
reaching the portal registry, the Scout queue path builder, and the Sheets
CRM. Each class of bypass here was reachable from open-board job data
(serpapi/remotive/remoteok/jobicy/wwr), so these are security regressions,
not style checks."""
from unittest.mock import MagicMock, patch

import pytest

import cv_tailor.portal.base as portal_base
from cv_tailor.job_sources import _best_company_url
from cv_tailor.portal.base import PortalAdapter, adapter_for, register_adapter
from cv_tailor.scout_queue import read_description, update_entry
from cv_tailor.sheets import build_row_from_fields, crm_mark_applied


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(portal_base, "_REGISTRY", [])
    return portal_base._REGISTRY


class _AshbyLike(PortalAdapter):
    hosts = ("jobs.ashbyhq.com",)
    name = "ashby-like"


# --- portal adapter allowlist ----------------------------------------------

def test_adapter_for_rejects_lookalike_hosts(clean_registry):
    register_adapter(_AshbyLike())
    # allowed host embedded inside an attacker-controlled hostname
    assert adapter_for("https://jobs.ashbyhq.com.evil.com/acme/apply") is None
    # allowed host only in the query string
    assert adapter_for("https://evil.com/apply?next=jobs.ashbyhq.com") is None
    # allowed host as userinfo in front of the real host
    assert adapter_for("https://jobs.ashbyhq.com@evil.com/acme/apply") is None
    # allowed host only in the path
    assert adapter_for("https://evil.com/jobs.ashbyhq.com/apply") is None


def test_adapter_for_accepts_exact_subdomain_port_and_case(clean_registry):
    adapter = register_adapter(_AshbyLike())
    assert adapter_for("https://jobs.ashbyhq.com/acme/engineer") is adapter
    assert adapter_for("https://JOBS.ASHBYHQ.COM/acme/engineer") is adapter
    assert adapter_for("https://jobs.ashbyhq.com:443/acme/engineer") is adapter
    assert adapter_for("https://eu.jobs.ashbyhq.com/acme/engineer") is adapter


# --- scout queue scan-date path segment -------------------------------------

def test_read_description_rejects_traversal_scan_date(tmp_path):
    with pytest.raises(ValueError):
        read_description("../../../etc", "job1", queue_dir=tmp_path)


def test_update_entry_rejects_scan_date_with_path_separator(tmp_path):
    with pytest.raises(ValueError):
        update_entry("2026-06-24/../..", "job1", lambda e: e, queue_dir=tmp_path)


def test_update_entry_rejects_calendar_invalid_scan_date(tmp_path):
    with pytest.raises(ValueError):
        update_entry("2026-13-99", "job1", lambda e: e, queue_dir=tmp_path)


def test_update_entry_valid_scan_date_still_reaches_queue(tmp_path):
    # a well-formed date passes validation and fails only on the missing queue
    with pytest.raises(FileNotFoundError):
        update_entry("2026-06-24", "job1", lambda e: e, queue_dir=tmp_path)


# --- job_sources board/ATS host matching ------------------------------------

def test_best_company_url_rejects_ats_lookalike_host():
    options = [{"link": "https://boards.greenhouse.io.evil.com/acme/1"}]
    picked = _best_company_url("Acme", options, "https://share.example/x")
    assert picked == "https://share.example/x"


def test_best_company_url_board_skip_requires_registrable_suffix():
    # a legit employer host that merely CONTAINS a board name must not be
    # skipped as if it were the job board itself
    options = [{"link": "https://acmegoogle.com/careers/1"}]
    picked = _best_company_url("Acmegoogle", options, "https://share.example/x")
    assert picked == "https://acmegoogle.com/careers/1"


# --- sheets formula-injection neutralization ---------------------------------

def test_build_row_from_fields_neutralizes_formula_prefixes():
    fields = {
        "job_meta": {
            "company": "=HYPERLINK(\"http://evil\",\"x\")",
            "role": "@evil",
            "location": "-2+3",
            "jd_url": "+IMPORTXML(1,1)",
        }
    }
    row = build_row_from_fields(fields, cv_path="/abs/cv.pdf")
    assert row[0].startswith("'=")
    assert row[1].startswith("'@")
    assert row[2].startswith("'-")
    assert row[3].startswith("'+")


def test_build_row_from_fields_leaves_benign_values_alone():
    fields = {
        "job_meta": {
            "company": "Acme",
            "role": "Engineer",
            "location": "Remote",
            "jd_url": "https://acme.example/jobs/1",
        }
    }
    row = build_row_from_fields(fields, cv_path="/abs/cv.pdf")
    assert row[0] == "Acme"
    assert row[3] == "https://acme.example/jobs/1"


def test_crm_mark_applied_neutralizes_formula_prefixes_on_append():
    worksheet = MagicMock()
    with patch("cv_tailor.sheets.get_pipeline_worksheet", return_value=worksheet), \
         patch("cv_tailor.sheets.update_status", side_effect=LookupError):
        ok = crm_mark_applied('=IMPORTRANGE("evil","A1")', "+SUM(1)", "https://x.example/jd")
    assert ok is True
    row = worksheet.append_row.call_args[0][0]
    assert row[0].startswith("'=")
    assert row[1].startswith("'+")
    assert row[3] == "https://x.example/jd"
