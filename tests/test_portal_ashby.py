# tests/test_portal_ashby.py
"""Ashby portal adapter: field mapping, required-question abort, blocker
recheck after navigation, and dry_run/armed submit semantics -- all
exercised with real headless chromium against the local fixture
(tests/fixtures/portal/ashby_form.html) served over http.server.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "portal"))

import cv_tailor.portal.ashby as ashby
import cv_tailor.portal.base as portal_base
from cv_tailor.portal import adapter_for, run_portal_application
from cv_tailor.portal.base import register_adapter
from cv_tailor.portal.ashby import AshbyAdapter
from serve import serve_fixtures

_PROFILE = {
    "contact": {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+44 20 7946 0958",
        "location": "London, UK",
    },
}
_ANSWERS = {
    "notice_period": "30 calendar days",
}


@pytest.fixture
def package(tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    cv_path = package_dir / "cv.pdf"
    cv_path.write_bytes(b"%PDF-1.4 fake\n")
    cover_letter_path = package_dir / "cover_letter.md"
    cover_letter_path.write_text("I would be a strong fit for this role.\n")
    return {
        "package_dir": str(package_dir),
        "cv_path": str(cv_path),
        "cover_letter_path": str(cover_letter_path),
    }


@pytest.fixture
def chromium_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _goto(page, base_url, variant=None):
    url = f"{base_url}/ashby_form.html"
    if variant:
        url += f"?variant={variant}"
    page.goto(url, wait_until="load")


# --- registry -----------------------------------------------------------------

def test_ashby_adapter_registered_for_jobs_ashbyhq_host():
    found = adapter_for("https://jobs.ashbyhq.com/xbowcareers/304f9f4e-477e-4d29-a39a-7c212738a0c8")

    assert isinstance(found, AshbyAdapter)


def test_ashby_adapter_hosts_and_name():
    adapter = AshbyAdapter()

    assert adapter.hosts == ("jobs.ashbyhq.com",)
    assert adapter.name == "ashby"


# --- happy path: dry_run fill --------------------------------------------------

def test_apply_dry_run_fills_contact_and_screening_fields(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        # Resume upload is asserted while the page is still open -- file
        # input values never appear in form_state.json (browsers never
        # expose them via .value, by design).
        uploaded = page.locator("#_systemfield_resume").evaluate("el => el.files.length")

    assert result.status == "filled"
    assert result.reason == ""
    assert uploaded == 1

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["_systemfield_name"] == "Ada Lovelace"
    assert state["_systemfield_email"] == "ada@example.com"
    assert state["_systemfield_phone"] == "+44 20 7946 0958"
    assert state["_systemfield_location"] == "London, UK"
    assert state["_systemfield_cover_letter"] == "I would be a strong fit for this role.\n"
    assert state["q_required_text"] == "30 calendar days"
    # "How did you hear about us?" has no grounded deterministic answer
    # and is optional -- left blank, not a failure.
    assert state["q_source"] == ""


def test_apply_dry_run_never_clicks_submit(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        confirmation_visible = page.locator("#confirmation").is_visible()

    assert confirmation_visible is False


# --- required-unanswerable -----------------------------------------------------

def test_apply_required_unanswerable_question_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="unanswerable")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason.startswith("unanswerable-required:")
    assert "project" in result.reason.lower()

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "aborted.png").exists()
    # "filled" is never reached on the abort path.
    assert not (evidence_dir / "filled.png").exists()


# --- captcha after navigation --------------------------------------------------

def test_apply_captcha_revealed_after_navigation_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="captcha")
        # The posting page itself has no captcha -- only the Application
        # tab's own content does, so an outer pre-dispatch blocker check
        # would pass here (proving this scenario exercises the adapter's
        # OWN post-navigation recheck, not just the C1 harness).
        assert ashby.detect_blockers(page) is None

        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "captcha"

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "captcha.png").exists()


# --- armed submit ---------------------------------------------------------------

def test_apply_armed_submit_returns_submitted_with_confirmation_evidence(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "submitted"
    assert result.reason == ""

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    assert (evidence_dir / "submitted.png").exists()


def test_apply_armed_no_confirmation_within_timeout_returns_needs_human(chromium_page, package, monkeypatch):
    monkeypatch.setattr(ashby, "CONFIRMATION_TIMEOUT_MS", 500)
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="nosubmit")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason == "no-confirmation"

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "no-confirmation.png").exists()


# --- end-to-end via run_portal_application (registry + dispatch wiring) --------

class _LocalAshbyAdapter(AshbyAdapter):
    """Same adapter, but claiming 127.0.0.1 instead of jobs.ashbyhq.com, so
    the dispatch-wiring smoke test can run against the local fixture
    server (the real host substring never matches an http://127.0.0.1 URL)."""

    hosts = ("127.0.0.1",)


def test_smoke_run_portal_application_dispatches_to_ashby_adapter(package, monkeypatch):
    monkeypatch.setattr(portal_base, "_REGISTRY", [])
    register_adapter(_LocalAshbyAdapter())
    entry_base = {"id": "job-1"}

    with serve_fixtures() as base_url:
        entry = {**entry_base, "apply_target": f"{base_url}/ashby_form.html"}
        result = run_portal_application(entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "filled"
    evidence_dir = Path(result.evidence_dir)
    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["_systemfield_name"] == "Ada Lovelace"
