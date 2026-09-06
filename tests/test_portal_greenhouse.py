# tests/test_portal_greenhouse.py
"""Greenhouse portal adapter: contact fill, resume/cover-letter upload,
screening-question enumeration, and the blocker/dry-run/submit flow --
exercised with a real headless chromium against the local fixture
(tests/fixtures/portal/greenhouse_form.html), which faithfully mirrors a
real Greenhouse Job Board posting's field ids/labels/aria attributes (see
that file's header comment for the two testability simplifications).

Calls `GreenhouseAdapter().apply()` directly rather than going through
`run_portal_application` so each test exercises exactly the adapter logic
under test (the base-level lifecycle/dispatch machinery is already covered
by tests/test_portal_base.py).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "portal"))

from cv_tailor.portal.greenhouse import GreenhouseAdapter, _split_name
from serve import serve_fixtures

PROFILE = {
    "contact": {
        "name": "Teodor-Cristian Lutoiu",
        "email": "contact@teodorlutoiu.com",
        "phone": "+40 725 697 859",
        "location": "Bucharest, Romania",
        "linkedin": "linkedin.com/in/teodorlc",
    }
}
ANSWERS: dict = {}


def _package(tmp_path) -> dict:
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    cv_path = pkg_dir / "cv.pdf"
    cv_path.write_bytes(b"%PDF-fake-cv-bytes")
    cover_letter_path = pkg_dir / "cover_letter.md"
    cover_letter_path.write_text("Dear hiring team, ...\n")
    return {
        "package_dir": str(pkg_dir),
        "cv_path": str(cv_path),
        "cover_letter_path": str(cover_letter_path),
    }


@pytest.fixture
def browser_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


# --- _split_name --------------------------------------------------------

def test_split_name_splits_on_first_space():
    assert _split_name("Teodor-Cristian Lutoiu") == ("Teodor-Cristian", "Lutoiu")


def test_split_name_handles_multi_word_last_name():
    assert _split_name("Mary Jane Watson") == ("Mary", "Jane Watson")


def test_split_name_handles_single_word_name():
    assert _split_name("Cher") == ("Cher", "")


def test_split_name_handles_empty_name():
    assert _split_name("") == ("", "")


# --- adapter registration -------------------------------------------------

def test_hosts_include_both_greenhouse_domains():
    adapter = GreenhouseAdapter()
    assert adapter.hosts == ("boards.greenhouse.io", "job-boards.greenhouse.io")
    assert adapter.name == "greenhouse"


# --- happy path: dry_run fill -------------------------------------------

def test_happy_fill_dry_run_returns_filled_with_evidence(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

    assert result.status == "filled"
    assert result.reason == ""
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()

    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["first_name"] == "Teodor-Cristian"
    assert state["last_name"] == "Lutoiu"
    assert state["email"] == "contact@teodorlutoiu.com"
    assert state["phone"] == "+40 725 697 859"
    # required custom question grounded from profile.contact.linkedin
    assert state["question_1002"] == "linkedin.com/in/teodorlc"
    # optional select question has no grounded answer (no LLM client wired
    # into the adapter) -- left blank, not a failure
    assert state["question_1001"] == ""


def test_happy_fill_uploads_resume_and_cover_letter(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html", wait_until="load")
        adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

        resume_files = browser_page.eval_on_selector(
            "#resume", "el => Array.from(el.files).map(f => f.name)"
        )
        cover_files = browser_page.eval_on_selector(
            "#cover_letter", "el => Array.from(el.files).map(f => f.name)"
        )

    assert resume_files == ["cv.pdf"]
    assert cover_files == ["cover_letter.md"]


# --- required-unanswerable -----------------------------------------------

def test_required_unanswerable_question_aborts_to_needs_human(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}
    # No linkedin on contact -> the required "LinkedIn Profile" question
    # has no grounded answer -- must abort before anything is submitted.
    profile = {"contact": {"name": "Teodor Lutoiu", "email": "t@example.com"}}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html", wait_until="load")
        result = adapter.apply(browser_page, entry, package, profile, ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "unanswerable-required:LinkedIn Profile"
    assert (Path(result.evidence_dir) / "aborted.png").exists()


# --- write-verified resume upload (C345) -----------------------------------

def test_resume_missing_cv_path_aborts_to_resume_upload_failed(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    package.pop("cv_path")
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "resume-upload-failed"
    assert not (Path(result.evidence_dir) / "filled.png").exists()


def test_resume_input_absent_aborts_to_resume_upload_failed(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html?noresume=1", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "resume-upload-failed"


# --- write-verified required screening answer (C345) -----------------------

def test_unwritable_required_question_aborts_to_needs_human(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        # The required LinkedIn question reverts every write: grounded from
        # profile.contact.linkedin, but never lands in the DOM.
        browser_page.goto(f"{base_url}/greenhouse_form.html?locked=1", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "unwritable-required:LinkedIn Profile"
    assert (Path(result.evidence_dir) / "aborted.png").exists()
    assert not (Path(result.evidence_dir) / "filled.png").exists()


# --- label fallback chain + radio/checkbox enumeration (C345) --------------

def test_radio_group_labeled_via_label_for_is_enumerated_and_answered(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        # ?radio=1 injects a required EEO "Gender" radio group whose label is
        # carried ONLY by <label for> (no aria-label): the fallback chain must
        # find it, and the deterministic EEO tier answers it with the decline
        # option (no LLM needed).
        browser_page.goto(f"{base_url}/greenhouse_form.html?radio=1", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

        decline_checked = browser_page.locator("#question_1003_d").is_checked()
        male_checked = browser_page.locator("#question_1003_m").is_checked()

    assert result.status == "filled"
    assert decline_checked is True
    assert male_checked is False


def test_unlabelled_required_control_aborts_to_needs_human(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        # ?unlabelled=1 injects a required control with no aria-label,
        # label[for], legend, or placeholder -- nothing to derive a label from.
        browser_page.goto(f"{base_url}/greenhouse_form.html?unlabelled=1", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "unlabelled-required-field"
    assert (Path(result.evidence_dir) / "aborted.png").exists()


# --- captcha blocker -------------------------------------------------------

def test_captcha_blocker_aborts_to_needs_human(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html?captcha=1", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "captcha"
    assert (Path(result.evidence_dir) / "blocked.png").exists()


# --- armed submit -----------------------------------------------------------

def test_armed_submit_reaches_confirmation(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=False)

    assert result.status == "submitted"
    assert result.reason == ""
    assert (Path(result.evidence_dir) / "submitted.png").exists()
    # dry_run's "filled" evidence is also present -- capture_evidence("filled")
    # runs unconditionally before the dry_run/armed branch.
    assert (Path(result.evidence_dir) / "filled.png").exists()


def test_dry_run_never_clicks_submit_even_when_confirmation_would_fail(tmp_path, browser_page):
    """dry_run=True must never touch the submit control -- proven here by
    pointing at the nosubmit=1 variant (which would time out if submit were
    ever clicked) and confirming we still get a clean "filled"."""
    adapter = GreenhouseAdapter()
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html?nosubmit=1", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=True)

    assert result.status == "filled"


def test_no_confirmation_within_timeout_returns_needs_human_and_never_retries(tmp_path, browser_page):
    adapter = GreenhouseAdapter()
    adapter.CONFIRM_TIMEOUT_MS = 300  # instance override -- don't actually wait 30s
    package = _package(tmp_path)
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        browser_page.goto(f"{base_url}/greenhouse_form.html?nosubmit=1", wait_until="load")
        result = adapter.apply(browser_page, entry, package, PROFILE, ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason == "no-confirmation"
    # confirmation never appeared, so this must never be reported as submitted
    assert not (Path(result.evidence_dir) / "submitted.png").exists()
