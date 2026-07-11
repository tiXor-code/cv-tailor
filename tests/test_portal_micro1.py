# tests/test_portal_micro1.py
"""Micro1 (jobs.micro1.ai) portal adapter: contact/phone/resume fill, the
Romania-country-select phone quirk, the second-step question flow (armed vs
handoff), blocker handling, and dry_run/submit semantics -- exercised with
real headless chromium against the local fixture
(tests/fixtures/portal/micro1_form.html), which faithfully mirrors a real
Micro1 posting's field names/classes (see that file's header comment for
provenance and the documented simplifications).

Calls `Micro1Adapter().apply()` directly rather than going through
`run_portal_application` so each test exercises exactly the adapter logic
under test (the base-level lifecycle/dispatch machinery is already covered
by tests/test_portal_base.py) -- same convention as the other three adapter
test modules.

One @pytest.mark.integration test (skipped unless RUN_INTEGRATION=1) does a
real dry_run fill against the live posting, screenshotting to
/tmp/micro1-live-check.png and never clicking Next.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "portal"))

from cv_tailor.portal import adapter_for
from cv_tailor.portal.micro1 import Micro1Adapter, _local_phone_digits
from serve import serve_fixtures

_PROFILE = {
    "contact": {
        "name": "Teodor-Cristian Lutoiu",
        "email": "contact@teodorlutoiu.com",
        "phone": "+40 725 697 859",
        "linkedin": "linkedin.com/in/teodorlc",
    },
}
_ANSWERS = {
    "availability_parttime": "Wednesday evenings and weekends",
}


@pytest.fixture
def package(tmp_path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    cv_path = package_dir / "cv.pdf"
    cv_path.write_bytes(b"%PDF-1.4 fake\n")
    return {"package_dir": str(package_dir), "cv_path": str(cv_path)}


@pytest.fixture
def chromium_page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _goto(page, base_url, variant=None):
    url = f"{base_url}/micro1_form.html"
    if variant:
        url += f"?variant={variant}"
    page.goto(url, wait_until="load")


# --- registry -----------------------------------------------------------------

def test_micro1_adapter_registered_for_jobs_micro1_ai_host():
    found = adapter_for("https://jobs.micro1.ai/post/c368cc32-d267-490e-abed-e9521cdf628e?referralCode=x")

    assert isinstance(found, Micro1Adapter)


def test_micro1_adapter_hosts_and_name():
    adapter = Micro1Adapter()

    assert adapter.hosts == ("jobs.micro1.ai",)
    assert adapter.name == "micro1"


# --- phone digit helper ---------------------------------------------------------

def test_local_phone_digits_strips_romania_dial_code():
    assert _local_phone_digits("+40 725 697 859") == "725697859"


def test_local_phone_digits_leaves_non_romanian_number_unchanged():
    assert _local_phone_digits("+1 555 555 5555") == "15555555555"


# --- happy path: dry_run fill --------------------------------------------------

def test_apply_dry_run_fills_contact_phone_linkedin_and_resume(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        # The phone widget and resume input carry no `name` attribute on the
        # real DOM (faithfully reproduced in the fixture), so neither shows
        # up in form_state.json -- read back live while the page is open,
        # same convention ashby's resume-count assertion uses.
        tel_value = page.locator("input.PhoneInputInput").input_value()
        uploaded = page.locator("input[type='file']").evaluate("el => el.files.length")

    assert result.status == "filled"
    assert result.reason == ""
    assert tel_value == "+40 725 697 859"
    assert uploaded == 1

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["first_name"] == "Teodor-Cristian"
    assert state["last_name"] == "Lutoiu"
    assert state["email_id"] == "contact@teodorlutoiu.com"
    assert state["linkedin_url"] == "https://linkedin.com/in/teodorlc"


def test_apply_dry_run_never_clicks_next(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        confirmation_visible = page.locator("#confirmation").is_visible()

    assert confirmation_visible is False


def test_apply_dry_run_switches_country_select_to_romania_before_filling(chromium_page, package):
    """?variant=notromania: the country select geo-defaults to United States
    (tel "+1") -- live-verified against the real posting that filling the
    tel input with a value carrying a DIFFERENT country's dial code while
    that country is selected is silently rejected, so the adapter must
    force the select to Romania first."""
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="notromania")
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        select_value = page.locator("select.PhoneInputCountrySelect").input_value()
        tel_value = page.locator("input.PhoneInputInput").input_value()

    assert result.status == "filled"
    assert select_value == "RO"
    assert tel_value == "+40 725 697 859"


# --- resume missing --------------------------------------------------------------

def test_apply_resume_missing_cv_path_aborts_to_resume_upload_failed(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    package_no_cv = {k: v for k, v in package.items() if k != "cv_path"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = Micro1Adapter().apply(page, entry, package_no_cv, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason.startswith("resume-upload-failed:")
    assert "no cv_path provided" in result.reason
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "aborted.png").exists()
    assert not (evidence_dir / "filled.png").exists()


# --- armed submit: happy path ----------------------------------------------------

def test_apply_armed_submit_default_variant_returns_submitted(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "submitted"
    assert result.reason == ""
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    assert (evidence_dir / "submitted.png").exists()


# --- second step: answered then submitted ----------------------------------------

def test_apply_armed_second_step_grounded_question_answered_then_submitted(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="step2answerable")
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "submitted"
    assert result.reason == ""
    assert (Path(result.evidence_dir) / "submitted.png").exists()


def test_apply_armed_second_step_unanswerable_required_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="step2unanswerable")
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason.startswith("unanswerable-required:")
    assert "planet" in result.reason.lower()
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "aborted.png").exists()
    assert not (evidence_dir / "submitted.png").exists()


# --- captcha mid-flow --------------------------------------------------------------

def test_apply_armed_captcha_revealed_after_next_click_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="captcha")
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason == "captcha"
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "captcha.png").exists()


# --- ambiguous no-confirmation -----------------------------------------------------

def test_apply_armed_no_confirmation_no_second_step_returns_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="nosubmit")
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason == (
        "no-confirmation: submission may have succeeded, VERIFY on the portal "
        "before applying manually"
    )
    assert (Path(result.evidence_dir) / "no-confirmation.png").exists()


# --- handoff: second step waits for a human, never auto-answers -------------------

def test_apply_handoff_second_step_notifies_and_times_out_without_auto_answering(
    chromium_page, package, monkeypatch
):
    """Unlike Ashby/Greenhouse/Lever (which never click submit themselves in
    handoff mode), Micro1's own initial "Next" click IS made by the adapter
    even in handoff -- the human/handoff boundary here is a second step of
    questions, which the adapter must never answer itself in handoff mode.
    With no fixture-side auto-continue and an immediate handoff timeout,
    this must degrade to needs_human without ever writing to the "hours"
    question field."""
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "0")
    page = chromium_page
    entry = {"id": "job-1", "company": "Micro1"}
    notified = []

    with serve_fixtures() as base_url:
        _goto(page, base_url, variant="step2answerable")
        result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS,
                                        dry_run=False, handoff=True, notify=notified.append)

        question_value = page.locator("[data-micro1-question='hours'] input").input_value()

    assert result.status == "needs_human"
    assert result.reason == "handoff-timeout: not submitted, form left as-is"
    assert question_value == ""  # never auto-answered
    assert notified == ["Micro1 micro1 form needs more info: waiting for a human to continue"]


# --- live fill-only check (real posting, dry_run only, nothing submitted) ---------

@pytest.mark.integration
def test_live_dry_run_fill_against_real_posting():
    """Real network call against the actual Micro1 posting used to build
    this adapter. dry_run=True only -- Next is never clicked, nothing is
    ever submitted. Screenshots to /tmp/micro1-live-check.png for manual
    review. Skipped unless RUN_INTEGRATION=1 (see tests/conftest.py)."""
    entry = {"id": "live-check"}
    package_dir = "/tmp/micro1-live-check-pkg"
    os.makedirs(package_dir, exist_ok=True)
    cv_path = os.path.join(package_dir, "cv.pdf")
    with open(cv_path, "wb") as f:
        f.write(b"%PDF-1.4 fake\n")
    package = {"package_dir": package_dir, "cv_path": cv_path}

    url = "https://jobs.micro1.ai/post/c368cc32-d267-490e-abed-e9521cdf628e"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="load", timeout=30_000)
            result = Micro1Adapter().apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)
            page.screenshot(path="/tmp/micro1-live-check.png", full_page=True)
            confirmation_visible = page.get_by_text("Thank you", exact=False).count() > 0
        finally:
            browser.close()

    assert result.status == "filled"
    assert confirmation_visible is False  # proves Next was never clicked
