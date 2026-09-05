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


# --- write-verified resume upload (C345) ---------------------------------------

def test_apply_resume_missing_cv_path_aborts_to_resume_upload_failed(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    package_no_cv = {k: v for k, v in package.items() if k != "cv_path"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = AshbyAdapter().apply(page, entry, package_no_cv, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    # Diagnostic reason: bare "resume-upload-failed" is now a prefix, with a
    # ": <what was checked and observed>" suffix so a live abort is a
    # one-read triage.
    assert result.reason.startswith("resume-upload-failed:")
    assert "no cv_path provided" in result.reason
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "aborted.png").exists()
    # never reaches "filled" -> never submits with a missing resume
    assert not (evidence_dir / "filled.png").exists()


def test_apply_resume_input_absent_aborts_to_resume_upload_failed(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="noresume")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    # Genuinely-missing input (task's own selector AND the input[type=file]
    # fallback both find nothing): the diagnostic says so explicitly.
    assert result.reason.startswith("resume-upload-failed:")
    assert "no file input found" in result.reason


# --- custom-uploader resume attach + verification (C345-followup) --------------
#
# Root cause of a live abort (jobs.ashbyhq.com/xbowcareers/09439fdb-a556-4d34-9043-
# eb9928bece8d, 2026-07-10): the real resume field is a custom drag-drop widget
# whose hidden <input type=file> node gets swapped by a React re-render shortly
# after the Application tab opens -- uploading before that settles silently loses
# the file with no error anywhere. _open_application_tab now waits for the SPA
# to settle before anything touches the form; these tests cover the widget shape
# itself (hidden input, dual verification signal) that motivated the fix.

def test_apply_hidden_resume_input_upload_succeeds(chromium_page, package):
    """The default fixture's resume input is visually hidden behind a styled
    dropzone button (mirrors the real widget's clip-path-off-screen trick --
    same technique confirmed live on the real jobs.ashbyhq.com DOM) -- proves
    the upload+verify path does not depend on the input having any on-screen
    footprint."""
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        resume_input = page.locator("#_systemfield_resume")
        box = resume_input.bounding_box()
        uploaded = resume_input.evaluate("el => el.files.length")

    # Playwright's own is_visible() still reports True for a 1x1px clipped
    # box (it only checks display/visibility/opacity, not clip-path), so the
    # real assertion of "visually hidden" is the negligible bounding box.
    assert box is not None and box["width"] <= 1 and box["height"] <= 1
    assert result.status == "filled"
    assert uploaded == 1


def test_apply_resume_swapped_input_node_verifies_via_filename(chromium_page, package):
    """?variant=resumeswap: the widget swaps the <input> for a fresh, unfilled
    clone right after the upload lands (same id), so a re-read of
    files.length is 0 -- the filename appearing in the dropzone is the only
    signal proving the upload actually landed, and the adapter must accept it."""
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="resumeswap")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        # Ground truth: the live input node genuinely has 0 files post-swap --
        # proves this test exercises the filename-fallback path, not files.length.
        post_swap_files = page.locator("#_systemfield_resume").evaluate("el => el.files.length")

    assert post_swap_files == 0
    assert result.status == "filled"
    assert result.reason == ""


# --- write-verified required screening answer (C345) ---------------------------

def test_apply_unwritable_required_field_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        # The required notice-period field silently reverts every write: the
        # answer is grounded (answers.notice_period) but never lands.
        _goto(page, base_url, variant="locked")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason.startswith("unwritable-required:")
    assert "notice period" in result.reason.lower()

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "aborted.png").exists()
    assert not (evidence_dir / "filled.png").exists()


# --- write-verified contact fields (C345) --------------------------------------

def test_apply_contact_email_unwritable_aborts_to_contact_fill_failed(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="lockedemail")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "contact-fill-failed"
    assert not (Path(result.evidence_dir) / "filled.png").exists()


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


def test_apply_armed_altconfirm_form_disappears_counts_as_submitted(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        # On submit the form is removed and a success panel with text that does
        # NOT contain "submitted"/"thank you" appears (no error banner). The
        # broadened detector must read the vanished form as success.
        _goto(page, base_url, variant="altconfirm")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "submitted"
    assert result.reason == ""
    assert (Path(result.evidence_dir) / "submitted.png").exists()


def test_apply_armed_no_confirmation_within_timeout_returns_needs_human(chromium_page, package, monkeypatch):
    monkeypatch.setattr(ashby, "CONFIRMATION_TIMEOUT_MS", 500)
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="nosubmit")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    # Broadened confirmation detection (C345): the no-confirmation reason now
    # carries the human-facing "verify on the portal" guidance.
    assert result.reason == (
        "no-confirmation: submission may have succeeded, VERIFY on the portal "
        "before applying manually"
    )
    assert result.reason.startswith("no-confirmation")

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "no-confirmation.png").exists()


def test_apply_armed_form_id_mismatch_never_false_reports_submitted(chromium_page, package, monkeypatch):
    """Phase C fix: signal 3 (form-vanish) assumed #application-form is the
    real Ashby id, never verified against a live posting. If the real id
    differs, that locator already reads 0 elements BEFORE any click -- the
    old code would misread "never matched to begin with" as "form vanished
    because the submit succeeded" and false-report every armed submit on
    such a posting as submitted. Pre-click presence must gate the signal."""
    monkeypatch.setattr(ashby, "CONFIRMATION_TIMEOUT_MS", 500)
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="formidmismatch")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason == (
        "no-confirmation: submission may have succeeded, VERIFY on the portal "
        "before applying manually"
    )


def test_apply_armed_normal_fixture_still_returns_submitted(chromium_page, package):
    """Regression guard for the Phase C fix above: the normal fixture's form
    DOES carry #application-form, so it must remain present pre-click and
    the ordinary armed-submit happy path must be unaffected."""
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "submitted"
    assert result.reason == ""


# --- handoff mode: headed fill, human does captcha + submit ------------------

def test_apply_handoff_never_submits_and_times_out_when_nothing_clicks(chromium_page, package, monkeypatch):
    """Direct proof that handoff mode never clicks submit itself: the
    DEFAULT fixture (no auto-click variant) is used, so the ONLY way
    #confirmation could ever appear is a real click -- none happens, so this
    must time out at needs_human with the confirmation panel still hidden."""
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "0")
    page = chromium_page
    entry = {"id": "job-1", "company": "Fixture Co"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS,
                                       dry_run=False, handoff=True, notify=None)

        confirmation_visible = page.locator("#confirmation").is_visible()

    assert result.status == "needs_human"
    assert result.reason == "handoff-timeout: not submitted, form left as-is"
    assert confirmation_visible is False

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    assert (evidence_dir / "handoff-timeout.png").exists()


def test_apply_handoff_confirmed_after_delayed_human_submit_returns_submitted(
    chromium_page, package, monkeypatch
):
    """?variant=handoffsubmit fires a fixture-only setTimeout that clicks
    #submit-btn ~300ms after page load, standing in for a human solving a
    captcha and clicking submit themselves -- nothing in the adapter's own
    handoff code path ever calls .click(). The poll (every 2s, per the
    handoff-mode contract) must notice the resulting confirmation text and
    return submitted with evidence."""
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "5")
    page = chromium_page
    entry = {"id": "job-1", "company": "Fixture Co"}
    notified = []

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="handoffsubmit")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS,
                                       dry_run=False, handoff=True, notify=notified.append)

        confirmation_visible = page.locator("#confirmation").is_visible()

    assert result.status == "submitted"
    assert result.reason == ""
    assert confirmation_visible is True
    assert notified == ["Fixture Co form filled and waiting: solve any captcha and click submit"]

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    assert (evidence_dir / "submitted.png").exists()


def test_apply_handoff_blocker_timeout_returns_handoff_specific_needs_human(
    chromium_page, package, monkeypatch
):
    """A captcha revealed after navigation (?variant=captcha, opened once the
    Application tab is clicked) aborts to plain needs_human("captcha")
    outside handoff mode (see
    test_apply_captcha_revealed_after_navigation_aborts_to_needs_human
    above). In handoff mode the SAME blocker instead goes through
    wait_for_blocker_clear; with an immediate handoff timeout (the
    fixture's captcha iframe is a static DOM marker that never clears on
    its own) it must degrade to the handoff-specific reason instead --
    proving resolve_blocker's handoff branch is actually wired into this
    adapter's real detect_blockers checkpoints, not just unit-tested in
    isolation (see test_portal_base.py's resolve_blocker tests for the
    cleared-continues-the-flow half of this behavior)."""
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "0")
    page = chromium_page
    entry = {"id": "job-1", "company": "Fixture Co"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="captcha")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS,
                                       dry_run=False, handoff=True, notify=None)

    assert result.status == "needs_human"
    assert result.reason == "handoff-timeout: captcha not solved"


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
