# tests/test_portal_lever.py
"""Lever portal adapter: field mapping, required-question abort, blocker
detection, and dry_run/armed submit semantics -- exercised with real
headless chromium against the local fixture
(tests/fixtures/portal/lever_form.html) served over http.server.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "portal"))

import cv_tailor.portal.base as portal_base
import cv_tailor.portal.lever as lever
from cv_tailor.portal import adapter_for, run_portal_application
from cv_tailor.portal.base import register_adapter
from cv_tailor.portal.lever import LeverAdapter
from serve import serve_fixtures

_PROFILE = {
    "contact": {
        "name": "Teodor-Cristian Lutoiu",
        "email": "contact@teodorlutoiu.com",
        "phone": "+40 725 697 859",
        "location": "Bucharest, Romania",
        "linkedin": "linkedin.com/in/teodorlc",
        "github": "github.com/tiXor-code",
    },
}
_ANSWERS = {
    "notice_period": "approximately 20 working days",
}


class _FakeClient:
    """Returns queued replies; records call count. Mirrors
    test_screening.py's fake -- screening.answer_question only cares about
    client.chat.completions.create(...).choices[0].message.content."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls += 1
        text = self._replies.pop(0)
        msg = SimpleNamespace(content=text)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


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


def _goto(page, base_url, **params):
    url = f"{base_url}/lever_form.html"
    if params:
        url += "?" + urlencode(params)
    page.goto(url, wait_until="load")


# --- registry -----------------------------------------------------------------

def test_lever_adapter_registered_for_jobs_lever_co_host():
    found = adapter_for("https://jobs.lever.co/ro/bde27362-0652-4d1a-bb8e-d6100ca20654")

    assert isinstance(found, LeverAdapter)


def test_lever_adapter_hosts_and_name():
    adapter = LeverAdapter()

    assert adapter.hosts == ("jobs.lever.co",)
    assert adapter.name == "lever"


def test_lever_adapter_default_client_is_none_deterministic_only():
    adapter = LeverAdapter()

    assert adapter.client is None
    assert adapter.deployment is None


# --- happy path: dry_run fill --------------------------------------------------

def test_apply_dry_run_fills_contact_links_resume_cover_letter_and_eeo_decline(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    client = _FakeClient(["Ministeru' Creativ"])
    adapter = LeverAdapter(client=client)

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

        # File input values never show up in form_state.json (browsers
        # never expose them via .value) -- assert while the page is open.
        uploaded = page.locator("input[name='resume']").evaluate("el => el.files.length")

    assert result.status == "filled"
    assert result.reason == ""
    assert uploaded == 1
    # Only "Current company" needs the LLM tier -- Gender resolves via the
    # deterministic EEO-decline policy, everything else is direct-filled.
    assert client.calls == 1

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["name"] == "Teodor-Cristian Lutoiu"
    assert state["email"] == "contact@teodorlutoiu.com"
    assert state["phone"] == "+40 725 697 859"
    assert state["location"] == "Bucharest, Romania"
    assert state["urls[LinkedIn]"] == "linkedin.com/in/teodorlc"
    assert state["urls[GitHub]"] == "github.com/tiXor-code"
    assert state["comments"] == "I would be a strong fit for this role.\n"
    assert state["org"] == "Ministeru' Creativ"
    assert state["eeo[gender]"] == "Decline to self-identify"


def test_apply_dry_run_never_clicks_submit(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    adapter = LeverAdapter(client=_FakeClient(["Ministeru' Creativ"]))

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)
        confirmation_present = page.locator("[data-qa='confirmation']").count()

    assert confirmation_present == 0


# --- required-unanswerable ------------------------------------------------------

def test_apply_required_unanswerable_question_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    adapter = LeverAdapter()  # no client -- deterministic tier only

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "unanswerable-required:Current company"

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "aborted.png").exists()
    # "filled" is never reached on the abort path.
    assert not (evidence_dir / "filled.png").exists()


# --- write-verified resume upload (C345) -----------------------------------------

def test_apply_resume_missing_cv_path_aborts_to_resume_upload_failed(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    package_no_cv = {k: v for k, v in package.items() if k != "cv_path"}
    adapter = LeverAdapter()  # abort happens before screening -> no client needed

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = adapter.apply(page, entry, package_no_cv, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "resume-upload-failed"
    assert not (Path(result.evidence_dir) / "filled.png").exists()


# --- write-verified required screening answer (C345) -----------------------------

def test_apply_unwritable_required_org_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    # org is grounded via the LLM tier, but the ?locked=1 field reverts writes.
    adapter = LeverAdapter(client=_FakeClient(["Ministeru' Creativ"]))

    with serve_fixtures() as base_url:
        _goto(page, base_url, locked="1")
        result = adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "unwritable-required:Current company"
    assert (Path(result.evidence_dir) / "aborted.png").exists()
    assert not (Path(result.evidence_dir) / "filled.png").exists()


# --- multi-field blocks, number kind, value-sourced selects (C345) ---------------

def test_discover_questions_enumerates_number_field_and_both_fields_of_a_block(chromium_page):
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, extra="1")
        found = lever.discover_questions(page)

    by_name = {name: q for q, name in found}
    # a type=number question is enumerated with kind "number"
    assert by_name["years_experience"].kind == "number"
    # a single .application-question block with TWO named controls yields BOTH
    assert "ref_name" in by_name
    assert "ref_email" in by_name
    assert by_name["ref_name"].kind == "text"
    assert by_name["ref_email"].kind == "text"


def test_apply_extra_coded_select_is_filled_by_value_not_visible_text(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    # org needs the LLM tier; every other extra field resolves deterministically
    # (EEO decline) or falls to the LLM and is told UNKNOWN -> left blank.
    adapter = LeverAdapter(client=_FakeClient(["Ministeru' Creativ"] + ["UNKNOWN"] * 10))

    with serve_fixtures() as base_url:
        _goto(page, base_url, extra="1")
        result = adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "filled"
    state = json.loads((Path(result.evidence_dir) / "form_state.json").read_text())
    # The coded EEO select's "Decline to self-identify" option has value="4"
    # and different visible text -- the decline flow must land the VALUE.
    assert state["eeo[gender_coded]"] == "4"
    # the original value==text EEO select still resolves to its value/text
    assert state["eeo[gender]"] == "Decline to self-identify"


def test_check_option_matches_value_containing_a_quote(chromium_page):
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url, extra="1")
        # An option value carrying an apostrophe must not break the locator:
        # the match is done in Python, never interpolated into a selector.
        ok = LeverAdapter._check_option(page, "radio", "referral_src", "A friend's referral")
        checked_value = page.locator("input[name='referral_src']:checked").get_attribute("value")

    assert ok is True
    assert checked_value == "A friend's referral"


# --- captcha ---------------------------------------------------------------------

def test_apply_captcha_wall_aborts_to_needs_human(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    adapter = LeverAdapter()

    with serve_fixtures() as base_url:
        _goto(page, base_url, captcha="1")
        result = adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "captcha"

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "blocked.png").exists()


# --- armed submit ------------------------------------------------------------------

def test_apply_armed_submit_returns_submitted_with_confirmation_evidence(chromium_page, package):
    page = chromium_page
    entry = {"id": "job-1"}
    adapter = LeverAdapter(client=_FakeClient(["Ministeru' Creativ"]))

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        result = adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "submitted"
    assert result.reason == ""

    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    assert (evidence_dir / "submitted.png").exists()


def test_apply_armed_no_confirmation_within_timeout_returns_needs_human(chromium_page, package, monkeypatch):
    monkeypatch.setattr(lever, "_CONFIRMATION_TIMEOUT_MS", 500)
    page = chromium_page
    entry = {"id": "job-1"}
    adapter = LeverAdapter(client=_FakeClient(["Ministeru' Creativ"]))

    with serve_fixtures() as base_url:
        _goto(page, base_url, nosubmit="1")
        result = adapter.apply(page, entry, package, _PROFILE, _ANSWERS, dry_run=False)

    assert result.status == "needs_human"
    assert result.reason == (
        "no-confirmation: submission may have succeeded, VERIFY on the portal "
        "before applying manually"
    )


# --- post-click submit errors (Phase C fix: ledger-row safety) -------------------
#
# A fake, duck-typed page (not real Playwright) isolates _submit_and_await_confirmation
# from the rest of apply()'s DOM interactions -- the same style as
# tests/test_portal_base.py's FakePage/FakeLocator stand-ins.

class _FakeSubmitLocator:
    def __init__(self):
        self.clicked = False

    @property
    def first(self):
        return self

    def click(self):
        self.clicked = True


class _FakeSubmitPage:
    def __init__(self, *, wait_error=None):
        self.locator_obj = _FakeSubmitLocator()
        self._wait_error = wait_error

    def locator(self, selector):
        return self.locator_obj

    def wait_for_selector(self, selector, timeout=None):
        if self._wait_error is not None:
            raise self._wait_error


def test_submit_post_click_non_timeout_playwright_error_returns_needs_human_not_failed(tmp_path):
    """Finding: the post-click wait used to catch ONLY PlaywrightTimeoutError,
    so a non-timeout PlaywrightError (page closed, navigation interrupted)
    escaped uncaught -> run_portal_application would report "failed" ->
    the orchestrator deletes the ledger row for a submission that may have
    already gone through. Must degrade to needs_human like a timeout does,
    keeping the row."""
    from playwright.sync_api import Error as PlaywrightError

    adapter = LeverAdapter()
    page = _FakeSubmitPage(wait_error=PlaywrightError("Target page, context or browser has been closed"))
    evidence_dir = tmp_path / "portal"

    result = adapter._submit_and_await_confirmation(page, evidence_dir)

    assert result.status == "needs_human"
    assert result.reason == (
        "no-confirmation: submission may have succeeded, VERIFY on the portal "
        "before applying manually"
    )
    assert page.locator_obj.clicked is True


def test_submit_post_click_timeout_still_returns_needs_human_same_reason(tmp_path):
    """Regression guard: a plain confirmation timeout (the original,
    already-handled case) must keep returning needs_human with the same
    standardized reason after the except clause was broadened."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    adapter = LeverAdapter()
    page = _FakeSubmitPage(wait_error=PlaywrightTimeoutError("Timeout 30000ms exceeded"))
    evidence_dir = tmp_path / "portal"

    result = adapter._submit_and_await_confirmation(page, evidence_dir)

    assert result.status == "needs_human"
    assert result.reason == (
        "no-confirmation: submission may have succeeded, VERIFY on the portal "
        "before applying manually"
    )


def test_submit_pre_click_error_still_returns_failed(tmp_path):
    """Regression guard: a PRE-click error (submit control missing/unclickable)
    is a genuine failure -- the click never happened, so nothing was
    submitted, and "failed" (ledger rollback) stays correct."""
    from playwright.sync_api import Error as PlaywrightError

    class _RaisingClickLocator:
        @property
        def first(self):
            return self

        def click(self):
            raise PlaywrightError("element not found")

    class _RaisingClickPage:
        def locator(self, selector):
            return _RaisingClickLocator()

    adapter = LeverAdapter()
    result = adapter._submit_and_await_confirmation(_RaisingClickPage(), tmp_path / "portal")

    assert result.status == "failed"
    assert result.reason.startswith("submit-click:")


# --- end-to-end via run_portal_application (registry + dispatch wiring) ----------

class _LocalLeverAdapter(LeverAdapter):
    """Same adapter, but claiming 127.0.0.1 instead of jobs.lever.co, so the
    dispatch-wiring smoke test can run against the local fixture server (the
    real host substring never matches an http://127.0.0.1 URL)."""

    hosts = ("127.0.0.1",)


def test_smoke_run_portal_application_dispatches_to_lever_adapter(package, monkeypatch):
    monkeypatch.setattr(portal_base, "_REGISTRY", [])
    register_adapter(_LocalLeverAdapter(client=_FakeClient(["Ministeru' Creativ"])))
    entry_base = {"id": "job-1"}

    with serve_fixtures() as base_url:
        entry = {**entry_base, "apply_target": f"{base_url}/lever_form.html"}
        result = run_portal_application(entry, package, _PROFILE, _ANSWERS, dry_run=True)

    assert result.status == "filled"
    evidence_dir = Path(result.evidence_dir)
    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["name"] == "Teodor-Cristian Lutoiu"


# --- discover_questions (pure DOM enumeration) ------------------------------------

def test_discover_questions_skips_direct_fill_fields_and_finds_org_and_gender(chromium_page):
    page = chromium_page

    with serve_fixtures() as base_url:
        _goto(page, base_url)
        found = lever.discover_questions(page)

    labels = {label: (kind, required, options) for (label, kind, required, options), _name in found}
    assert "Full name" not in labels  # direct-filled, never re-discovered
    assert "Email" not in labels
    assert "Phone" not in labels
    assert "Current location" not in labels

    assert labels["Current company"] == ("text", True, ())
    assert labels["Gender"][0] == "select"
    assert labels["Gender"][1] is False
    assert "Decline to self-identify" in labels["Gender"][2]
