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
    # Fictional and deliberately LOWERCASE, so the radio test also pins the
    # case-insensitive match against the "Careers Page" option.
    "how_heard": "careers page",
    # Fictional, like every value here -- this file is tracked in a PUBLIC
    # repo (see the work_authorization note below for the near-miss that
    # established the rule). Deliberately not a round, plausible figure.
    "salary_fulltime_gross_eur_month": 4321,
    # Grounding material for work-authorization questions. DELIBERATELY
    # FICTIONAL -- this file is tracked in a PUBLIC repo, and the rule here is
    # that real answers.yaml values never appear in one. The first draft of
    # this line copied the opening clause of his actual answer, which the leak
    # canary would have MISSED (it matches the full value, not a prefix), so
    # the guard would not have saved it.
    #
    # _work_auth_answer maps this to a Yes/No option ONLY when the question
    # label names a jurisdiction; a label like RobCo's ("...the country for
    # which you are applying") names none, so it deliberately falls through to
    # the LLM tier rather than guessing -- which is the path this fixture
    # exercises.
    "work_authorization": "Fixture citizen of Examplestan; authorised to work "
                          "in Examplestan without sponsorship.",
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


def test_apply_resume_input_absent_aborts_to_resume_upload_failed(chromium_page, package, monkeypatch):
    page = chromium_page
    entry = {"id": "job-1"}
    # This fixture genuinely has no file input, so the form-ready wait can
    # only ever time out here -- shortened so the abort path stays fast
    # (same contract as CONFIRMATION_TIMEOUT_MS in the armed tests).
    monkeypatch.setattr(ashby, "FORM_READY_TIMEOUT_MS", 500)

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
    assert result.reason == "contact-fill-failed:email"
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


def test_number_salary_box_is_filled_and_the_currency_stated_elsewhere(chromium_page, package):
    """Live everfield 2026-09-16: the REQUIRED salary box is <input
    type="number">, which silently refuses "4321 EUR gross per month" -- so
    fill() wrote nothing, verify_filled failed, and the run aborted
    unwritable-required having sent nothing.

    Teodor's policy: write the number, and state the currency in a free-text
    box on the same form, because a non-euro employer reads a naked 4321 about
    4x wrong and it would be SUBMITTED rather than parked."""
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="salarynumber")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)
        salary = page.locator("#q_salary_number").input_value()
        letter = page.locator("#_systemfield_cover_letter").input_value()
        other = page.locator("#q_anything_else").input_value()

    assert result.status == "filled", result.reason
    assert salary == "4321", "a number box can only take the bare figure"
    # The cover letter is the natural place to say it -- it is a letter to the
    # employer, and the adapter has already written the letter into that box,
    # so the note must APPEND rather than clobber it.
    assert "I would be a strong fit" in letter, "the cover letter was overwritten"
    assert "4321" in letter and "EUR" in letter, f"currency never stated: {letter!r}"
    assert other == "", "the cover letter is preferred over a generic free-text box"


def test_number_salary_box_with_nowhere_to_state_the_currency_parks(chromium_page, package):
    """The other half of the policy, and the one that protects real money.

    everfield's only <textarea> is the hidden, unlabelled g-recaptcha-response
    and its "Cover letter" is a FILE upload, so there is nowhere to say "EUR".
    With no suitable box the run must PARK rather than submit a bare figure --
    and must leave no half-filled debris behind, the lesson from the location
    combobox."""
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="salarynumbernonote")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)
        salary = page.locator("#q_salary_number").input_value()

    assert result.status == "needs_human"
    assert result.reason == (
        "unanswerable-required:What would be your salary expectation for this role?")
    assert salary == "", "parked, so nothing should have been typed into the box"


def test_apply_armed_explicit_refusal_proves_nothing_was_submitted(
        chromium_page, package, monkeypatch):
    """Live RobCo evidence 2026-09-16 (portal/no-confirmation.png): the page
    read "We couldn't submit your application ... flagged as possible spam",
    yet the run returned the AMBIGUOUS no-confirmation reason. That reason is
    in neither NO_SUBMIT_REASONS nor its prefixes, so the pre-inserted ledger
    row was KEPT -- marking robco applied forever and blocking every sibling
    role through norm_key, for an application the portal itself says never
    happened.

    An explicit refusal is PROOF nothing was sent, so the reason must be one
    proves_no_submission() recognises. It must still never retry the submit:
    retrying a spam-flagged form is permanently out of scope, and a retry is
    the one way a duplicate application could escape."""
    from cv_tailor.apply_policy import proves_no_submission

    monkeypatch.setattr(ashby, "CONFIRMATION_TIMEOUT_MS", 500)
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="spamrejected")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason.startswith("submit-rejected")
    assert proves_no_submission(result.reason), (
        "an explicitly refused submit must roll the ledger row back")
    assert not result.reason.startswith("no-confirmation")


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


# --- late-rendering application route -----------------------------------------

def test_apply_waits_for_late_rendering_form_instead_of_aborting(chromium_page, package):
    """Ashby's router renders the application form asynchronously: for a
    window after the route loads the page is blank but for a spinner, with
    no form and no input[type=file] anywhere.

    Every Ashby park in production landed in exactly that window -- Flip
    GmbH (2026-09-10), Checkly (09-12) and Sardine (09-16) each aborted
    with "resume-upload-failed: no file input found", each with an
    aborted.png showing a blank/spinner page and form_state.json == {}.

    The adapter must wait for the form to exist rather than reading the DOM
    while it is still rendering. networkidle cannot cover this -- nothing is
    in flight during a client-side render.
    """
    page = chromium_page
    entry = {"id": "job-late-render"}

    with serve_fixtures() as base_url:
        page.goto(f"{base_url}/ashby_posting/", wait_until="load")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        uploaded = page.locator("#_systemfield_resume").evaluate("el => el.files.length")
        landed = page.url

    assert result.status == "filled", result.reason
    assert uploaded == 1
    assert landed.rstrip("/").endswith("/application")

    state = json.loads((Path(result.evidence_dir) / "form_state.json").read_text())
    assert state["_systemfield_name"] == "Ada Lovelace"
    assert state["_systemfield_email"] == "ada@example.com"


def test_apply_reaches_the_form_when_the_tab_click_never_navigates(chromium_page, package, monkeypatch):
    """The second live failure mode. Measured 2026-09-16 against the real
    deepgram posting: #job-application-form is present as a link to
    /application, the job URL serves 0 file inputs, /application serves 2 --
    and a dry-run still ended on the JOB url having filled nothing, because
    the tab click never navigated and _open_application_tab swallows the
    error.

    A click can fail for reasons the adapter cannot control. The route is the
    contract, so the adapter must navigate to <job-url>/application itself.
    """
    page = chromium_page
    entry = {"id": "job-no-tab"}
    monkeypatch.setattr(ashby, "FORM_READY_TIMEOUT_MS", 1500)

    with serve_fixtures() as base_url:
        page.goto(f"{base_url}/ashby_posting_notab/", wait_until="load")
        result = AshbyAdapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        uploaded = page.locator("#_systemfield_resume").evaluate("el => el.files.length")
        landed = page.url

    assert result.status == "filled", result.reason
    assert uploaded == 1
    assert landed.rstrip("/").endswith("/application")


# --- open-ended required question ----------------------------------------------

def _tiered_client(prose: str, calls: list | None = None):
    """A client that behaves like the real one does on a motivation question:
    UNKNOWN to the FACTUAL screening prompt (nothing in profile/answers grounds
    it), prose only to the COMPOSE prompt. Keyed off the real constant, so this
    cannot drift from what the module actually sends."""
    from types import SimpleNamespace

    from cv_tailor.screening import COMPOSE_SYSTEM_PROMPT

    def create(**kwargs):
        messages = kwargs.get("messages") or []
        system = messages[0]["content"] if messages else ""
        if calls is not None:
            calls.append(system)
        reply = prose if system == COMPOSE_SYSTEM_PROMPT else "UNKNOWN"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _factual_client(reply: str):
    """A client that answers the FACTUAL screening prompt (not the compose one).

    A Yes/No toggle is a radio question, so it goes through the factual tier --
    _tiered_client answers UNKNOWN there by design, which is right for a
    motivation question and wrong for this one. The real LLM is instructed to
    reply with the EXACT text of one option, and _kind_gate then requires an
    exact match, so "Yes" is what production actually produces here.
    """
    from types import SimpleNamespace

    def create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))])

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def test_open_ended_required_question_is_answered_from_the_letter(chromium_page, package):
    """The last thing that parked real applications.

    Measured live 2026-09-16 with a real Azure client: Sardine (ashby) stopped
    on "What excites you about the opportunity to join Sardine?" and
    saas.group (greenhouse) on "Shortly describe your most impactful AI feature
    you shipped end to end" -- both REQUIRED free text, with every other field
    on the form already filled and the resume attached.

    The factual tier can only answer UNKNOWN there, so the adapter has to hand
    the screening module the letter that ships with THIS application and let it
    compose from that.
    """
    page = chromium_page
    systems: list[str] = []
    client = _tiered_client(
        "Fixture Co works on payment integrations, which is the same end-to-end "
        "work I have been doing, including the reconciliation nobody wants to "
        "debug at 2am.",
        systems,
    )

    with serve_fixtures() as base_url:
        page.goto(f"{base_url}/ashby_openended.html", wait_until="load")
        result = AshbyAdapter().apply(page, {"id": "job-open"}, package, _PROFILE,
                                      _ANSWERS, dry_run=True, client=client)
        # Attribute form, not "#<id>": the fixture's id is a digit-leading
        # UUID (as Ashby's really are), and "#5d69..." is not a valid CSS
        # selector -- which is the very bug this test now covers.
        answered = page.locator(
            '[id="5d69bc56-0ca7-49e3-b539-ce4c829fd8fa"]').input_value()

    assert result.status == "filled", result.reason
    assert "payment" in answered.lower()


def test_armed_submit_finds_the_real_ashby_submit_button(chromium_page, package):
    """The last step, and the one no test ever exercised against the real shape.

    Every armed test here runs against ashby_form.html, whose submit button
    carries id="submit-btn". Live Ashby does not: measured 2026-09-16 on a real
    application page, #submit-btn matches 0 elements, there is no
    button[type=submit] and no input[type=submit] -- the control is a plain
    <button> with no id, no type, a hashed CSS-module class and the text
    "Submit Application".

    So the scheduled run on 2026-09-16T13:23Z got a real application all the
    way through -- discovered, scored, approved, assembled, resume attached,
    screening answered, free text composed -- and then spent 119 seconds
    waiting to click an element that does not exist, failing at the very last
    step (camunda).
    """
    page = chromium_page
    # Keep the red fast: live this was a 119s actionability timeout.
    page.set_default_timeout(3000)
    client = _tiered_client(
        "Fixture Co works on payment integrations, which is the same end-to-end "
        "work I have been doing, including the reconciliation nobody wants to "
        "debug at 2am."
    )

    with serve_fixtures() as base_url:
        page.goto(f"{base_url}/ashby_openended.html", wait_until="load")
        result = AshbyAdapter().apply(page, {"id": "job-armed"}, package, _PROFILE,
                                      _ANSWERS, dry_run=False, client=client)

    assert result.status == "submitted", result.reason


# --- requiredness is not in the HTML ------------------------------------------
#
# The most dangerous defect found on 2026-09-16. Live on the robco posting,
# autopilot filled the form and clicked submit with THREE required fields
# empty (Location, Mobility & Relocation, Legal Eligibility to Work), because
# _question_for_wrapper reads requiredness from the HTML `required` attribute
# and Ashby never sets it -- nor aria-required, nor an asterisk in the label
# text. Every required field therefore read as OPTIONAL, and "ungrounded
# optional" means leave it blank and carry on.
#
# Parking beats submitting a half-filled application under his name.

def test_a_question_required_by_label_class_is_not_treated_as_optional(chromium_page, package):
    """Ashby's own marker: a CSS-module class containing _required_ on the label."""
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="requiredbyclass")
        result = AshbyAdapter().apply(page, {"id": "job-req-class"}, package,
                                      _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human", result.reason
    assert result.reason == "unanswerable-required:How did you hear about us?"


def test_a_question_required_by_rendered_asterisk_is_not_treated_as_optional(chromium_page, package):
    """The durable signal: the asterisk is ::after pseudo-content, invisible to
    inner_text but present in computed style. Class hashes churn between Ashby
    deploys; the rendered asterisk does not."""
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="requiredbystar")
        result = AshbyAdapter().apply(page, {"id": "job-req-star"}, package,
                                      _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human", result.reason
    assert result.reason == "unanswerable-required:How did you hear about us?"


# --- Yes/No toggle widgets -----------------------------------------------------
#
# What now stands between the system and a completed application. Measured live
# on the robco posting 2026-09-16: "Mobility & Relocation" and "Legal
# Eligibility to Work" are REQUIRED and are rendered as two <button>s inside
# .ashby-application-form-input-yesno, backed by a hidden checkbox -- not a
# <select>, not a radio group. _question_for_wrapper sees the hidden <input>,
# calls it kind="text", and fill_field writes nothing.
#
# The answers are already grounded (answers.work_authorization,
# answers.relocation), so this is a widget-driving problem, not an answering
# one. State change observed on clicking "Yes":
#     before  Yes:aria-pressed=false  No:aria-pressed=false  checkbox=False
#     after   Yes:aria-pressed=true   No:aria-pressed=false  checkbox=True

def test_a_yes_no_toggle_answered_no_is_written_and_verified(chromium_page, package):
    """"No" is a real answer, not a failed write. andercore (2026-09-17) parked
    unwritable-required on "Will you now or in the future require visa
    sponsorship to work legally in Germany?" while its own widget showed
    No:aria-pressed="true" -- the click had worked. _toggle demanded the backing
    checkbox be checked, and that checkbox tracks YES, so every No failed.

    Three of the five unwritable-required parks on 2026-09-24 were visa
    questions, whose truthful answer for him is always No."""
    page = chromium_page
    client = _factual_client("No")

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="yesno")
        result = AshbyAdapter().apply(page, {"id": "job-yesno-no"}, package, _PROFILE,
                                      _ANSWERS, dry_run=True, client=client)

        wrapper = page.locator('[data-field-path="q_source"]')
        yes_pressed = wrapper.locator("button", has_text="Yes").first.get_attribute("aria-pressed")
        no_pressed = wrapper.locator("button", has_text="No").first.get_attribute("aria-pressed")
        backing_checked = wrapper.locator("input[type=checkbox]").first.is_checked()

    assert result.status == "filled", result.reason
    assert no_pressed == "true"
    assert yes_pressed == "false"
    assert backing_checked is False, "the checkbox tracks Yes, so No leaves it unchecked"


def test_a_yes_no_toggle_is_answered_and_verified(chromium_page, package):
    page = chromium_page
    # RobCo's real label names no jurisdiction, so the deterministic
    # work-auth rule defers to the LLM -- exactly as it will live.
    client = _factual_client("Yes")

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="yesno")
        result = AshbyAdapter().apply(page, {"id": "job-yesno"}, package, _PROFILE,
                                      _ANSWERS, dry_run=True, client=client)

        wrapper = page.locator('[data-field-path="q_source"]')
        yes_pressed = wrapper.locator("button", has_text="Yes").first.get_attribute("aria-pressed")
        no_pressed = wrapper.locator("button", has_text="No").first.get_attribute("aria-pressed")
        backing_checked = wrapper.locator("input[type=checkbox]").first.is_checked()

    assert result.status == "filled", result.reason
    assert yes_pressed == "true"
    assert no_pressed == "false"
    assert backing_checked is True


def test_an_unanswerable_yes_no_toggle_parks_rather_than_leaving_it_blank(chromium_page, package):
    """No client means the deterministic tier only, which cannot ground this
    question -- a REQUIRED toggle with no grounded answer must park, never be
    left silently empty the way it was before requiredness was detected."""
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="yesno")
        result = AshbyAdapter().apply(page, {"id": "job-yesno-blank"}, package,
                                      _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human", result.reason
    assert result.reason.startswith("unanswerable-required:")


# --- location combobox ---------------------------------------------------------
#
# The last required widget standing between a robco-shaped posting and a
# completed application, and the subtlest: fill_field DOES write the value, so
# nothing looks wrong -- but the control treats typed text as a search query and
# discards it on blur unless an option is selected. Measured live on robco:
#
#     after fill            'Bucharest, Romania'   <- fill DOES write
#     after blur (no pick)  ''                     <- discarded
#     after type + pick     'Bucharest, Romania'
#     after pick + blur     'Bucharest, Romania'   <- survives
#
# Location is NOT in the screening path (_systemfield_location is in
# _HANDLED_FIELD_IDS), so the contact fill owns it -- and _verify_contact only
# read-verifies name + email, which is why losing it was invisible and robco
# reached the submit click with a required field empty.

def test_location_combobox_value_survives_moving_to_the_next_field(chromium_page, package):
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="locationcombo")
        result = AshbyAdapter().apply(page, {"id": "job-loc"}, package, _PROFILE,
                                      _ANSWERS, dry_run=True)
        landed = page.locator("#_systemfield_location").input_value()

    assert result.status == "filled", result.reason
    assert landed == _PROFILE["contact"]["location"]


def test_location_combobox_waits_through_a_slow_loading_list(chromium_page, package):
    """Live oyster 2026-09-19 (portal/aborted.png) and Cohere 2026-09-18 both
    parked contact-fill-failed, and the screenshot caught the location dropdown
    frozen on "Loading...". The same Cohere form committed the location in
    0.5s when re-run on 2026-09-24, so this is LATENCY, not a broken widget.

    The placeholder is itself a role=option row, so waiting for "any option"
    was satisfied instantly by a row that can never match, and the exact-match
    scan then gave up and cleared the box. A slow list must be waited out."""
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="locationcombo&slow=1")
        result = AshbyAdapter().apply(page, {"id": "job-loc-slow"}, package, _PROFILE,
                                      _ANSWERS, dry_run=True)
        landed = page.locator("#_systemfield_location").input_value()

    assert result.status == "filled", result.reason
    assert landed == _PROFILE["contact"]["location"]


def test_a_real_radio_group_question_is_answered(chromium_page, package):
    """Live Sardine (score 8, 2026-09-24): "How did you hear about Sardine?" is a
    genuine radio group -- options Linkedin / Job Board / In-person event /
    Referral / ... -- and it parked unwritable-required with the right answer
    in hand. Only Ashby's Yes/No BUTTON widget was ever recognised as a choice
    question; a real radio group fell to the text branch and targeted an id no
    element carries."""
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="radiogroup")
        result = AshbyAdapter().apply(page, {"id": "job-radio"}, package, _PROFILE,
                                      _ANSWERS, dry_run=True)
        wrap = page.locator('[data-field-path="q_heard"]')
        picked = [wrap.locator("label", has_text=o).first.get_attribute("for")
                  for o in ("Careers Page", "Linkedin")]
        careers_checked = page.locator(f'[id="{picked[0]}"]').is_checked()
        linkedin_checked = page.locator(f'[id="{picked[1]}"]').is_checked()

    assert result.status == "filled", result.reason
    assert careers_checked is True, "'careers page' must select the 'Careers Page' option"
    assert linkedin_checked is False


def test_a_custom_location_combobox_question_is_committed(chromium_page, package):
    """Live ElevenLabs (score 8, 2026-09-20) parked unwritable-required:Location.
    Its Location is a CUSTOM question (uuid field path) rendered as a combobox,
    so the contact path's combobox handling never saw it and the screening path
    plain-filled it -- a value Ashby discards on blur unless an option is
    picked."""
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="customlocation")
        result = AshbyAdapter().apply(page, {"id": "job-custom-loc"}, package, _PROFILE,
                                      _ANSWERS, dry_run=True)
        landed = page.locator('[data-field-path="q_custom_location"] input').input_value()

    assert result.status == "filled", result.reason
    assert landed == _PROFILE["contact"]["location"]


def test_location_combobox_that_offers_no_match_aborts_rather_than_submitting_blank(
        chromium_page, package):
    """A required field we cannot commit must abort, never sail on as "filled".
    The fixture's decoy-only variant offers no exact match for the profile's
    location."""
    page = chromium_page
    profile = {"contact": dict(_PROFILE["contact"], location="Nowhere-On-Sea, Atlantis")}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="locationcombo&nomatch=1")
        result = AshbyAdapter().apply(page, {"id": "job-loc-nomatch"}, package,
                                      profile, _ANSWERS, dry_run=True)

    assert result.status == "needs_human", result.reason
    # The reason NAMES the field. A bare "contact-fill-failed" on Cohere and
    # oyster took a screenshot read to discover it was the location, not name
    # or email.
    assert result.reason == "contact-fill-failed:location"
