# src/cv_tailor/portal/ashby.py
"""Ashby (jobs.ashbyhq.com) portal adapter.

Ashby postings show an "Overview" tab by default; the application form
lives behind an "Application" tab (`#job-application-form`, a real Ashby
id) that must be clicked to reveal the form panel. Field ids/names
(`_systemfield_name`, `_systemfield_email`, `_systemfield_resume`) mirror a
real posting's DOM -- see tests/fixtures/portal/ashby_form.html for the
provenance note and the documented simplifications.

Flow: detect_blockers (in case the posting page itself is walled) -> open
the Application tab -> detect_blockers again (a captcha can appear only
once the form panel renders) -> upload the CV (real Ashby re-renders the
form on resume selection, so this happens before any typed field to avoid
losing it to that re-render) -> fill contact fields from profile.contact
-> paste the cover letter if the form has that field -> enumerate
remaining screening questions -> answer_question each (a
REQUIRED question with no grounded answer aborts to needs_human before
anything is submitted) -> capture "filled" evidence -> dry_run stops here;
armed clicks submit and waits up to CONFIRMATION_TIMEOUT_MS for a
confirmation signal, capturing "submitted" evidence on success or
degrading to needs_human("no-confirmation") on timeout (never retried --
the submission may have gone through).
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from cv_tailor.portal.base import (
    PortalAdapter,
    PortalResult,
    capture_evidence,
    detect_blockers,
    fill_field,
    register_adapter,
)
from cv_tailor.screening import Answer, Question, answer_question

# How long to wait for a post-submit confirmation signal before degrading
# to needs_human("no-confirmation"). A module constant (not a hardcoded
# literal) so tests can monkeypatch it short instead of waiting 30s.
CONFIRMATION_TIMEOUT_MS = 30_000

_APPLICATION_TAB_SELECTOR = "#job-application-form"
_RESUME_SELECTOR = "#_systemfield_resume"
_COVER_LETTER_SELECTOR = "#_systemfield_cover_letter"
_SUBMIT_SELECTOR = "#submit-btn"
_CONFIRMATION_TEXT = "been submitted"

# Contact fields filled directly from profile.contact, keyed by the
# selector used to fill them. Ashby doesn't have a single universal set of
# systemfield ids across every org's board config, so link fields are
# included defensively: fill_field is a no-op (returns False) when the
# selector matches nothing, so postings without these fields are unaffected.
_CONTACT_FIELD_SELECTORS = (
    ("#_systemfield_name", "name"),
    ("#_systemfield_email", "email"),
    ("#_systemfield_phone", "phone"),
    ("#_systemfield_location", "location"),
    ("#_systemfield_linkedin", "linkedin"),
    ("#_systemfield_github", "github"),
    ("#_systemfield_website", "website"),
)

# ids handled explicitly above (contact, resume, cover letter) -- excluded
# from the "remaining screening questions" enumeration so they are never
# double-answered by the screening module.
_HANDLED_FIELD_IDS = {sel.lstrip("#") for sel, _ in _CONTACT_FIELD_SELECTORS} | {
    _RESUME_SELECTOR.lstrip("#"),
    _COVER_LETTER_SELECTOR.lstrip("#"),
}


class AshbyAdapter(PortalAdapter):
    hosts = ("jobs.ashbyhq.com",)
    name = "ashby"

    def apply(self, page, entry: dict, package: dict, profile: dict,
              answers: dict, *, dry_run: bool) -> PortalResult:
        evidence_dir = Path(package["package_dir"]) / "portal"

        blocker = detect_blockers(page)
        if blocker:
            capture_evidence(page, evidence_dir, blocker)
            return PortalResult(status="needs_human", reason=blocker, evidence_dir=str(evidence_dir))

        self._open_application_tab(page)

        blocker = detect_blockers(page)
        if blocker:
            capture_evidence(page, evidence_dir, blocker)
            return PortalResult(status="needs_human", reason=blocker, evidence_dir=str(evidence_dir))

        # Upload the resume FIRST, before any typed field: on the real
        # Ashby form, selecting a resume triggers a client-side re-render
        # (resume parsing) that detaches and rebuilds the whole form,
        # silently wiping any values typed beforehand. Uploading first
        # means later fills land on the settled, post-parse DOM.
        self._upload_resume(page, package.get("cv_path"))

        contact = (profile or {}).get("contact", {}) or {}
        for selector, key in _CONTACT_FIELD_SELECTORS:
            fill_field(page, selector, contact.get(key, ""))

        self._fill_cover_letter(page, package.get("cover_letter_path"))

        aborted = self._answer_remaining_questions(page, profile, answers)
        if aborted is not None:
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason=aborted, evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "filled")

        if dry_run:
            return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))

        return self._submit_and_await_confirmation(page, evidence_dir)

    # --- navigation ---------------------------------------------------------

    def _open_application_tab(self, page) -> None:
        """Click into the Application tab if present. Never raises -- some
        postings may already be on the application route (e.g. a direct
        apply_target URL), in which case the tab selector legitimately
        matches nothing and filling proceeds against the current page."""
        try:
            tab = page.locator(_APPLICATION_TAB_SELECTOR)
            if tab.count() > 0:
                tab.first.click()
        except PlaywrightError:
            pass

    # --- filling --------------------------------------------------------------

    def _upload_resume(self, page, cv_path) -> bool:
        if not cv_path:
            return False
        try:
            locator = page.locator(_RESUME_SELECTOR)
            if locator.count() == 0:
                return False
            locator.set_input_files(cv_path)
        except PlaywrightError:
            return False
        # Ashby's real form kicks off an async resume-parse on upload and
        # re-renders the form when it completes, silently wiping anything
        # typed in that window. Give it a moment to settle before the
        # caller starts filling other fields; a timeout here just means
        # the network never went idle (nothing else in-flight) -- proceed
        # anyway rather than blocking the whole apply on it.
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightError:
            pass
        return True

    def _fill_cover_letter(self, page, cover_letter_path) -> bool:
        if not cover_letter_path:
            return False
        try:
            text = Path(cover_letter_path).read_text()
        except OSError:
            return False
        return fill_field(page, _COVER_LETTER_SELECTOR, text)

    # --- screening questions ----------------------------------------------------

    def _answer_remaining_questions(self, page, profile: dict, answers: dict) -> str | None:
        """Enumerate field-entry wrappers not already handled, answer each
        via the screening module, and fill the form. Returns a
        needs_human reason string on a required-unanswerable question,
        else None."""
        try:
            wrappers = page.locator("[data-field-path]").all()
        except PlaywrightError:
            return None

        for wrapper in wrappers:
            field_id = wrapper.get_attribute("data-field-path")
            if not field_id or field_id in _HANDLED_FIELD_IDS:
                continue

            question, selector, options = self._question_for_wrapper(wrapper, field_id)
            if question is None:
                continue

            answer = answer_question(question, profile, answers)

            # answer_question with no client (deterministic tier only) can
            # return None for ANY unmatched question, not just required
            # ones (see test_screening.py::test_no_client_optional_no_deterministic_match_still_returns_none) --
            # required/optional policy is the caller's job here.
            if answer is None or not answer.value:
                if question.required:
                    return f"unanswerable-required:{question.label}"
                continue  # optional + ungrounded -> leave blank, not a failure

            if question.kind == "select":
                if answer.value not in options:
                    # Deterministic tier isn't options-aware; a value that
                    # doesn't match one of this select's exact options is
                    # not safely fillable -- same policy as ungrounded.
                    if question.required:
                        return f"unanswerable-required:{question.label}"
                    continue
                try:
                    page.locator(selector).select_option(label=answer.value)
                except PlaywrightError:
                    if question.required:
                        return f"unanswerable-required:{question.label}"
            else:
                fill_field(page, selector, answer.value)

        return None

    def _question_for_wrapper(self, wrapper, field_id: str):
        """Return (Question, css_selector, options) for one field-entry
        wrapper, or (None, None, None) when the wrapper's shape isn't a
        fillable question (never raises)."""
        selector = f"#{field_id}"
        try:
            label_el = wrapper.locator("label").first
            label = label_el.inner_text().strip() if label_el.count() > 0 else field_id

            select_el = wrapper.locator("select")
            if select_el.count() > 0:
                required = select_el.first.get_attribute("required") is not None
                options = tuple(
                    opt.inner_text().strip()
                    for opt in select_el.first.locator("option").all()
                    if (opt.get_attribute("value") or "") != ""
                )
                return Question(label=label, kind="select", required=required, options=options), selector, options

            textarea_el = wrapper.locator("textarea")
            if textarea_el.count() > 0:
                required = textarea_el.first.get_attribute("required") is not None
                return Question(label=label, kind="textarea", required=required), selector, ()

            input_el = wrapper.locator("input")
            if input_el.count() > 0:
                required = input_el.first.get_attribute("required") is not None
                return Question(label=label, kind="text", required=required), selector, ()
        except PlaywrightError:
            pass
        return None, None, None

    # --- submission -----------------------------------------------------------

    def _submit_and_await_confirmation(self, page, evidence_dir: Path) -> PortalResult:
        try:
            page.locator(_SUBMIT_SELECTOR).first.click()
        except PlaywrightError as exc:
            capture_evidence(page, evidence_dir, "submit-failed")
            return PortalResult(status="failed", reason=f"submit-click: {exc}", evidence_dir=str(evidence_dir))

        try:
            page.get_by_text(_CONFIRMATION_TEXT, exact=False).first.wait_for(
                state="visible", timeout=CONFIRMATION_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            capture_evidence(page, evidence_dir, "no-confirmation")
            return PortalResult(status="needs_human", reason="no-confirmation", evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "submitted")
        return PortalResult(status="submitted", reason="", evidence_dir=str(evidence_dir))


ASHBY_ADAPTER = register_adapter(AshbyAdapter())
