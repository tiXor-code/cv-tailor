# src/cv_tailor/portal/ashby.py
"""Ashby (jobs.ashbyhq.com) portal adapter.

Ashby postings show an "Overview" tab by default; the application form
lives behind an "Application" tab (`#job-application-form`, a real Ashby
id) that must be clicked to reveal the form panel. Field ids/names
(`_systemfield_name`, `_systemfield_email`, `_systemfield_resume`) mirror a
real posting's DOM -- see tests/fixtures/portal/ashby_form.html for the
provenance note and the documented simplifications.

Flow: detect_blockers (in case the posting page itself is walled) -> open
the Application tab (and wait for the SPA to settle -- see
_open_application_tab, a real posting swaps the whole panel's DOM,
including the resume file input, in a re-render shortly after the tab
click) -> detect_blockers again (a captcha can appear only once the form
panel renders) -> upload the CV (real Ashby re-renders the form again on
resume selection, so this happens before any typed field to avoid losing
it to that re-render) -> fill contact fields from profile.contact
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

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError

from cv_tailor.portal.base import (
    PortalAdapter,
    PortalResult,
    capture_evidence,
    detect_blockers,
    fill_field,
    handoff_timeout_s,
    register_adapter,
    id_selector,
    resolve_blocker,
    screening_context,
    submit_rejected,
    SUBMIT_REJECTED_REASON,
    verify_file_attached,
    verify_filled,
)
from cv_tailor.screening import Question, answer_question

# How long to wait for a post-submit confirmation signal before degrading
# to needs_human("no-confirmation"). A module constant (not a hardcoded
# literal) so tests can monkeypatch it short instead of waiting 30s.
CONFIRMATION_TIMEOUT_MS = 30_000

# How long to wait for the application form to actually RENDER after the
# application route opens, before giving up and letting the normal
# diagnostic abort fire. A module constant (not a hardcoded literal) so
# tests can monkeypatch it short, same contract as CONFIRMATION_TIMEOUT_MS.
#
# Ashby's router renders the form panel asynchronously: for a window after
# the route loads, the page is blank but for a centred spinner, carrying no
# form and no input[type=file] at all. networkidle does NOT cover this --
# nothing is in flight during a client-side render, so it returns
# immediately on a still-blank page. Every Ashby park in production landed
# in exactly that window (Flip GmbH 2026-09-10, Checkly 09-12, Sardine
# 09-16: all three aborted "no file input found" with form_state.json == {}
# and an aborted.png showing a blank/spinner page).
FORM_READY_TIMEOUT_MS = 15_000

# How long to wait for a combobox's suggestion list after writing the value.
#
# Measured live on the robco posting 2026-09-16: a programmatic fill() alone
# opens the listbox after 0.8s -- simulating keystrokes was never required.
# 3s is nearly 4x the measured latency.
#
# Kept deliberately tight rather than generous: a control can advertise
# role="combobox" and never suggest anything, and this wait is pure dead time
# on that path before the plain-write fallback. An over-long value is not
# harmlessly cautious -- it stalls the whole fill, which in handoff mode hands
# the page a wide window to change underneath the adapter.
# How long to wait for a combobox's REAL options. Was 3s. Live oyster
# (2026-09-19) and Cohere (2026-09-18) both parked contact-fill-failed with the
# dropdown caught on "Loading..."; the same Cohere form committed in 0.5s when
# re-run on 2026-09-24. So this is latency under load, and the ceiling only
# costs time on a failure path.
COMBOBOX_OPTION_TIMEOUT_MS = 10_000
# ...but ONLY once the list has shown signs of life. A field that shows no row
# at all within this window is treated as never-suggesting, the old fast path:
# without it every such field paid the full 10s, which also broke the handoff
# fixture whose stand-in human stops clicking after ~4s.
COMBOBOX_FIRST_OPTION_MS = 3_000
# A placeholder row is itself role=option on these widgets, so "any option is
# visible" is satisfied instantly by a row that can never match.
_LOADING_OPTION_RE = re.compile(r"^\s*(loading|searching)\b", re.I)

_APPLICATION_TAB_SELECTOR = "#job-application-form"
_RESUME_SELECTOR = "#_systemfield_resume"
# Ashby's real resume field is a custom drag-drop widget: a hidden
# input[type=file] behind a styled "Upload File" button. Board configs
# can differ per org, so if the known id ever misses, fall back to any
# file input on the page (see _find_resume_locator).
_RESUME_FALLBACK_SELECTOR = "input[type='file']"
_COVER_LETTER_SELECTOR = "#_systemfield_cover_letter"
# Live Ashby renders the submit control as a plain <button> with NO id, NO
# type attribute and a hashed CSS-module class. Measured 2026-09-16 on the
# cohere and camunda application pages:
#     #submit-btn                           -> 0
#     button[type=submit]                   -> 0
#     input[type=submit]                    -> 0
#     button:has-text('Submit Application') -> 1 (visible, on both)
#
# "#submit-btn" is what ashby_form.html uses, so every armed test passed while
# a real application spent 119 seconds waiting to click an element that does
# not exist. The scheduled run on 2026-09-16T13:23Z took a camunda application
# all the way -- discovered, scored, approved, assembled, resume attached,
# screening answered, free text composed -- and lost it at the final click.
_SUBMIT_SELECTOR = ("#submit-btn, button[type=submit], input[type=submit], "
                    "button:has-text('Submit Application')")

# Broadened confirmation detection (armed path). A real Ashby submit can signal
# success several ways -- the canonical "been submitted" banner, a generic
# thank-you, or (increasingly) an SPA that simply swaps the form out for a
# success view and/or routes to a new path. Any one of these visible signals is
# treated as success; only the total absence of all of them within the cap is a
# no-confirmation. The reason string below is what a human sees, so it must say
# the submission MIGHT have gone through.
_CONFIRMATION_RE = re.compile(
    r"been submitted|application (?:received|submitted)|thank you for applying|"
    r"thanks for applying|successfully submitted",
    re.I,
)
# An error/validation banner means the form is still open on a failure, NOT a
# vanished-because-succeeded form -- suppresses the form-disappeared signal.
_ERROR_BANNER_SELECTOR = (
    "[role='alert'], .ashby-application-form-error, .error, .form-error, [aria-invalid='true']"
)
_NO_CONFIRMATION_REASON = (
    "no-confirmation: submission may have succeeded, VERIFY on the portal "
    "before applying manually"
)
# The explicit-refusal detector (_SUBMIT_REJECTED_RE / SUBMIT_REJECTED_REASON /
# submit_rejected) lives in portal.base: ALL FOUR adapters had this same gap,
# and one regex with four call sites beats four copies that drift.

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
              answers: dict, *, dry_run: bool, client: Any = None,
              deployment: str | None = None, handoff: bool = False,
              notify: Any = None) -> PortalResult:
        evidence_dir = Path(package["package_dir"]) / "portal"

        blocker = detect_blockers(page)
        if blocker:
            result = resolve_blocker(page, blocker, evidence_dir, stage=blocker,
                                      handoff=handoff, notify=notify)
            if result is not None:
                return result

        self._open_application_tab(page)

        blocker = detect_blockers(page)
        if blocker:
            result = resolve_blocker(page, blocker, evidence_dir, stage=blocker,
                                      handoff=handoff, notify=notify)
            if result is not None:
                return result

        # Upload the resume FIRST, before any typed field: on the real
        # Ashby form, selecting a resume triggers a client-side re-render
        # (resume parsing) that detaches and rebuilds the whole form,
        # silently wiping any values typed beforehand. Uploading first
        # means later fills land on the settled, post-parse DOM.
        #
        # The upload is write-VERIFIED (el.files length, or the dropzone's
        # post-upload UI state -- see _upload_and_verify_resume) -- a missing
        # cv_path, a selector that matches nothing, an upload error, or a file
        # that never actually attached must abort to needs_human BEFORE any
        # field is typed and long before any armed submit, rather than
        # silently applying with no resume. The failure reason carries a
        # diagnostic (what was checked, what was observed) so a live abort is
        # a one-read triage.
        uploaded, upload_detail = self._upload_and_verify_resume(page, package.get("cv_path"))
        if not uploaded:
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason=f"resume-upload-failed: {upload_detail}",
                                 evidence_dir=str(evidence_dir))

        contact = (profile or {}).get("contact", {}) or {}
        for selector, key in _CONTACT_FIELD_SELECTORS:
            value = contact.get(key, "")
            if not value:
                continue
            target = self._contact_selector(page, selector)
            if target is None:
                continue        # this board has no such field: a non-event
            # A combobox (Ashby's Location) discards typed text on blur unless
            # an option is picked, so fill_field silently loses it. Every other
            # board and field keeps the plain path.
            if self._is_combobox(page, target):
                self._combobox(page, target, value)
            else:
                fill_field(page, target, value)

        # The two universally-required contact fields (name, email) are
        # write-verified: if either was given but did not land in the DOM,
        # abort rather than submit a form missing the applicant's identity.
        unwritten_contact = self._verify_contact(page, contact)
        if unwritten_contact is not None:
            capture_evidence(page, evidence_dir, "aborted")
            # The reason NAMES the field: a bare "contact-fill-failed" on
            # Cohere and oyster took a screenshot read to learn it was location.
            return PortalResult(status="needs_human",
                                 reason=f"contact-fill-failed:{unwritten_contact}",
                                 evidence_dir=str(evidence_dir))

        self._fill_cover_letter(page, package.get("cover_letter_path"))

        # The letter written for THIS job travels with the questions: a
        # required open-ended one ("what excites you about joining X") has no
        # answer in profile/answers, so without it the factual tier can only
        # say UNKNOWN and the whole application parks.
        aborted = self._answer_remaining_questions(
            page, profile, answers, client=client, deployment=deployment,
            context=screening_context(package))
        if aborted is not None:
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason=aborted, evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "filled")

        if dry_run:
            return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))

        if handoff:
            return self._await_handoff_submission(page, entry, evidence_dir, notify)

        return self._submit_and_await_confirmation(page, evidence_dir)

    # --- navigation ---------------------------------------------------------

    def _open_application_tab(self, page) -> None:
        """Click into the Application tab if present, then wait for the SPA
        to settle. Never raises -- some postings may already be on the
        application route (e.g. a direct apply_target URL), in which case
        the tab selector legitimately matches nothing and filling proceeds
        against the current page.

        The settle wait is load-bearing, not cosmetic: live-verified against
        a real posting, the Application panel's DOM -- including the resume
        file input -- gets swapped out by a React re-render ~150-300ms after
        the tab click (confirmed by tagging the pre-click input node and
        polling for the tag to survive). Uploading before that re-render
        settles attaches the file to a node that gets discarded moments
        later, which was silently losing the resume with no error anywhere
        (this is the root cause of the resume-upload-failed abort seen on a
        live XBow/Ashby run before this fix). A timeout here just means the
        network never went idle (nothing else in flight) -- proceed anyway
        rather than blocking the whole apply on it."""
        try:
            tab = page.locator(_APPLICATION_TAB_SELECTOR)
            if tab.count() > 0:
                tab.first.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightError:
                    pass
        except PlaywrightError:
            pass

        # Then wait for the form itself to exist. `state="attached"` is
        # load-bearing: Ashby's real resume input is a visually-hidden
        # input[type=file] clipped off-screen behind a drag-drop widget, so
        # the default "visible" wait would time out on a perfectly good
        # form. A timeout here is not fatal -- fall through and let
        # _upload_and_verify_resume produce its own diagnostic abort, which
        # names what was checked and what was observed.
        try:
            page.wait_for_selector(
                f"{_RESUME_SELECTOR}, {_RESUME_FALLBACK_SELECTOR}",
                state="attached", timeout=FORM_READY_TIMEOUT_MS,
            )
        except PlaywrightError:
            pass

        # The tab is not the contract -- the ROUTE is. Measured 2026-09-16 on
        # two live boards (deepgram, sardine): the job URL serves zero file
        # inputs and <job-url>/application serves the form.
        #
        # Depending on the tab click is what kept failing, silently, in three
        # different ways: the tab had not rendered yet when tab.count() was
        # read (so no click happened at all), the click could time out on
        # actionability, or it could be intercepted -- and every one of those
        # is swallowed by the except above. A live dry-run against the real
        # deepgram posting finished still on the JOB url having filled
        # nothing. So if the form still is not here, go to the route
        # ourselves rather than trusting a click to have worked.
        if self._no_file_input(page):
            self._goto_application_route(page)

    @staticmethod
    def _no_file_input(page) -> bool:
        """True when the page exposes no file input at all right now.

        Never raises: a selector-engine error reads as "no form here", which
        sends the caller to the application route -- the safe direction,
        since navigating to a page that already has the form is a no-op."""
        try:
            return page.locator(_RESUME_FALLBACK_SELECTOR).count() == 0
        except PlaywrightError:
            return True

    def _goto_application_route(self, page) -> None:
        """Navigate to <current-url>/application and wait for the form.

        The query is preserved -- aggregator apply links carry utm params and
        dropping them changes the URL the board sees -- and the fragment is
        dropped. A URL already on /application is left alone, so an
        apply_target that points straight at the form is never doubled.

        Never raises: a failure here falls through to
        _upload_and_verify_resume's own diagnostic abort, which names what
        was checked and what was observed."""
        try:
            parts = urlsplit(page.url)
            if parts.path.rstrip("/").endswith("/application"):
                return
            target = urlunsplit((parts.scheme, parts.netloc,
                                 parts.path.rstrip("/") + "/application",
                                 parts.query, ""))
            page.goto(target, wait_until="load")
            page.wait_for_selector(
                f"{_RESUME_SELECTOR}, {_RESUME_FALLBACK_SELECTOR}",
                state="attached", timeout=FORM_READY_TIMEOUT_MS,
            )
        except PlaywrightError:
            pass

    # --- filling --------------------------------------------------------------

    def _upload_and_verify_resume(self, page, cv_path) -> tuple[bool, str]:
        """Upload the resume and confirm it actually attached. Returns
        (True, "") on success or (False, diagnostic) where diagnostic names
        what was checked and what was observed -- so a live abort is a
        one-read triage instead of the bare "resume-upload-failed" this used
        to return.

        Verification ORs three signals, all re-read fresh AFTER the upload
        settles (never from a handle captured before it): verify_file_attached
        on the known systemfield selector, the resolved locator's own `files`
        property (covers the fallback-selector case, where the known
        selector legitimately matches nothing), and the dropzone's
        post-upload UI state (uploaded filename text, or a remove/delete
        control). The UI-state signal exists because a custom uploader can
        swap its underlying <input> node for a fresh, unfilled one right
        after an upload while keeping the "uploaded" state in its own
        component state -- files.length on the fresh node would read 0 even
        though the widget correctly registered the upload."""
        if not cv_path:
            return False, "no cv_path provided"

        locator = self._find_resume_locator(page)
        if locator is None:
            return False, (
                f"no file input found ({_RESUME_SELECTOR} and "
                f"{_RESUME_FALLBACK_SELECTOR} fallback both matched 0 elements)"
            )

        try:
            locator.set_input_files(cv_path)
        except PlaywrightError as exc:
            return False, f"set_input_files raised {type(exc).__name__}: {exc}"

        # Ashby's real form kicks off an async resume-parse on upload and
        # re-renders the form when it completes, silently wiping anything
        # typed in that window. Give it a moment to settle before reading
        # back or letting the caller fill other fields; a timeout here just
        # means the network never went idle (nothing else in-flight) --
        # proceed anyway rather than blocking the whole apply on it.
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightError:
            pass

        filename = Path(cv_path).name
        files_attached = verify_file_attached(page, _RESUME_SELECTOR) or self._locator_has_files(locator)
        if files_attached or self._resume_filename_visible(page, filename):
            return True, ""

        return False, (
            f"set_input_files ran against a matched file input but neither "
            f"files.length>0 nor the filename '{filename}' appeared anywhere "
            f"on the page after settling to networkidle"
        )

    def _find_resume_locator(self, page):
        """Return a Locator for the resume file input: the known systemfield
        id when present, else any input[type=file] on the page (Ashby board
        configs can differ per org) -- preferring one whose accept attribute
        mentions pdf, then one whose nearby container mentions resume, else
        just the first candidate. None when nothing matches at all. Never
        raises -- a selector engine error on one candidate just skips it and
        tries the next."""
        try:
            primary = page.locator(_RESUME_SELECTOR)
            if primary.count() > 0:
                return primary.first
        except PlaywrightError:
            pass

        try:
            fallback = page.locator(_RESUME_FALLBACK_SELECTOR)
            count = fallback.count()
        except PlaywrightError:
            return None
        if count == 0:
            return None
        if count == 1:
            return fallback.first

        for i in range(count):
            candidate = fallback.nth(i)
            try:
                accept = (candidate.get_attribute("accept") or "").lower()
            except PlaywrightError:
                continue
            if "pdf" in accept:
                return candidate
        for i in range(count):
            candidate = fallback.nth(i)
            try:
                container_text = candidate.evaluate(
                    "el => (el.closest('div, form') || el.parentElement || el).innerText || ''"
                )
            except PlaywrightError:
                continue
            if "resume" in container_text.lower():
                return candidate
        return fallback.first

    @staticmethod
    def _locator_has_files(locator) -> bool:
        """Re-query `locator` (never a stale handle -- Locators resolve the
        live DOM on every call) for a non-empty `files` list. False on any
        error, never raises."""
        try:
            return bool(locator.evaluate("el => !!(el.files && el.files.length > 0)"))
        except PlaywrightError:
            return False

    @staticmethod
    def _resume_filename_visible(page, filename: str) -> bool:
        """True if the uploaded filename is visibly rendered anywhere on the
        page, or a remove/delete control is present -- the dropzone's
        post-upload UI state, used as an alternate confirmation signal to
        files.length (see _upload_and_verify_resume's docstring for why)."""
        try:
            if page.get_by_text(filename, exact=False).count() > 0:
                return True
        except PlaywrightError:
            pass
        try:
            remove_control = page.locator(
                "button:has-text('Remove'), button:has-text('Delete'), "
                "[aria-label*='remove' i], [aria-label*='delete' i]"
            )
            return remove_control.count() > 0
        except PlaywrightError:
            return False

    @staticmethod
    def _verify_contact(page, contact: dict) -> str | None:
        """Read back name + email + location after filling. Returns the first
        field key whose non-empty grounded value did not land in the DOM, else
        None.

        Location joined this list because it is the one contact field rendered
        as a combobox: fill_field reports success and the value is discarded on
        blur, so without a read-back the adapter believed a required field was
        filled when it was empty -- which is how robco reached the submit click
        on 2026-09-16.
        """
        for selector, key in (("#_systemfield_name", "name"),
                              ("#_systemfield_email", "email"),
                              ("#_systemfield_location", "location")):
            expected = (contact or {}).get(key, "")
            if not expected:
                continue
            target = AshbyAdapter._contact_selector(page, selector)
            if target is None:
                continue        # field absent on this board -> nothing to lose
            if not verify_filled(page, target, expected):
                return key
        return None

    def _fill_cover_letter(self, page, cover_letter_path) -> bool:
        if not cover_letter_path:
            return False
        try:
            text = Path(cover_letter_path).read_text()
        except OSError:
            return False
        return fill_field(page, _COVER_LETTER_SELECTOR, text)

    # --- screening questions ----------------------------------------------------

    def _answer_remaining_questions(self, page, profile: dict, answers: dict, *,
                                     client: Any = None, deployment: str | None = None,
                                     context: dict | None = None) -> str | None:
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

            answer = answer_question(question, profile, answers, client=client,
                                      deployment=deployment, context=context)

            # answer_question with no client (deterministic tier only) can
            # return None for ANY unmatched question, not just required
            # ones (see test_screening.py::test_no_client_optional_no_deterministic_match_still_returns_none) --
            # required/optional policy is the caller's job here.
            if answer is None or not answer.value:
                if question.required:
                    return f"unanswerable-required:{question.label}"
                continue  # optional + ungrounded -> leave blank, not a failure

            # A note means the CONTROL cannot carry the whole answer: a salary
            # <input type="number"> holds 4321 but not "EUR gross per month",
            # and a non-euro employer reads the naked figure ~4x wrong. Policy
            # (Teodor, 2026-09-16): state the currency in a free-text box, or
            # PARK -- never submit the bare number.
            #
            # Resolved BEFORE anything is typed, so a park leaves no
            # half-filled debris behind (the location-combobox lesson).
            note_box = self._note_target(page) if answer.note else None
            if answer.note and note_box is None:
                if question.required:
                    return f"unanswerable-required:{question.label}"
                continue

            if question.kind == "radio":
                # A Yes/No toggle: the screening tier must return one of the
                # button labels verbatim (_kind_gate enforces that for radio),
                # so anything else is as unsafe as an ungrounded answer.
                if answer.value not in options:
                    if question.required:
                        return f"unanswerable-required:{question.label}"
                    continue
                if wrapper.locator(".ashby-application-form-input-yesno").count() > 0:
                    written = self._toggle(wrapper, answer.value)
                else:
                    written = self._radio(wrapper, answer.value)
            elif question.kind == "select":
                if answer.value not in options:
                    # Deterministic tier isn't options-aware; a value that
                    # doesn't match one of this select's exact options is
                    # not safely fillable -- same policy as ungrounded.
                    if question.required:
                        return f"unanswerable-required:{question.label}"
                    continue
                written = self._select(page, selector, answer.value)
            elif self._is_combobox(page, selector):
                # A CUSTOM question rendered as a combobox. Live ElevenLabs
                # (score 8, 2026-09-20) parked unwritable-required:Location on
                # one: the contact path's combobox handling only covers
                # _systemfield_location, so this was plain-filled. Worse, a
                # plain fill PASSES verify_filled -- it reads the typed text
                # before focus leaves, and Ashby discards it on blur -- so the
                # fixture showed a required Location reported "filled" and
                # left empty.
                written = self._combobox(page, selector, answer.value)
            else:
                written = fill_field(page, selector, answer.value) and \
                    verify_filled(page, selector, answer.value)

            # A REQUIRED answer that did not verify (readonly/reverting field,
            # a stale selector) must abort -- "grounded" is not "written".
            # Optional fields stay best-effort: a failed optional write is a
            # blank field, not a needs_human.
            if not written and question.required:
                return f"unwritable-required:{question.label}"

            # The figure landed; now the currency must too, or the employer
            # reads a bare number. A failure here is a park, not a shrug.
            if written and answer.note and note_box is not None:
                if not self._append_note(note_box, answer.note) and question.required:
                    return f"unanswerable-required:{question.label}"

        return None

    @staticmethod
    def _contact_selector(page, selector: str) -> str | None:
        """Resolve a contact field to a selector that actually matches.

        Ashby is inconsistent about ids. Measured live on the robco posting
        2026-09-16: #_systemfield_name and #_systemfield_email each matched 1,
        while #_systemfield_location matched ZERO -- that input carries no id
        and no name, only role="combobox", inside a wrapper whose
        data-field-path IS "_systemfield_location" (its <label for> points at
        an id nothing on the page has).

        Returns the id selector when it matches, else the wrapper-scoped
        input, else None -- and None means the board simply does not have this
        field, which is a non-event rather than a failure. Conflating "absent"
        with "lost" is what made five unrelated tests fail on fixtures that
        legitimately have no location field at all.
        """
        try:
            if page.locator(selector).count() > 0:
                return selector
        except PlaywrightError:
            return None
        scoped = f'[data-field-path="{selector.lstrip("#")}"] input'
        try:
            return scoped if page.locator(scoped).count() > 0 else None
        except PlaywrightError:
            return None

    @staticmethod
    def _is_combobox(page, selector: str) -> bool:
        """True when this control is a combobox rather than a plain input.
        Never raises -- an unreadable control reads as "not a combobox", which
        keeps the plain fill_field path for every other board."""
        try:
            loc = page.locator(selector)
            if loc.count() == 0:
                return False
            return (loc.first.get_attribute("role") or "") == "combobox"
        except PlaywrightError:
            return False

    @staticmethod
    def _combobox(page, selector: str, value: str) -> bool:
        """Type `value`, pick the option matching it EXACTLY, and confirm it
        survived losing focus.

        Ashby's Location control treats typed text as a SEARCH QUERY and
        discards it on blur unless an option was actually selected. Measured
        live on the robco posting 2026-09-16:

            after fill            'Bucharest, Romania'   <- fill DOES write
            after blur (no pick)  ''                     <- discarded
            after type + pick     'Bucharest, Romania'
            after pick + blur     'Bucharest, Romania'   <- survives

        So fill_field "succeeded" and the value vanished the moment the next
        contact field took focus -- with _verify_contact checking only name and
        email, that loss was invisible, and robco reached the submit click with
        a required field empty.

        The match must be EXACT: the live list offered 'Bucharest, Romania'
        twice alongside 'Bucharzewo, Miedzychod County', so clicking the first
        row is not good enough. Returns False on anything unexpected, so the
        caller aborts rather than submitting the field blank.
        """
        try:
            box = page.locator(selector).first
            box.click()
            # ATOMIC write, never per-character typing. Measured live: fill()
            # alone opens the suggestion list after 0.8s, so simulating
            # keystrokes bought nothing and cost 1.2s -- a window in which the
            # page can submit or re-render out from under the interaction and
            # strand a half-typed value. That is precisely what happened in
            # the handoff fixture, whose auto-clicker submitted mid-typing and
            # left 'Lo' behind in the box.
            box.fill(value)

            options = page.locator("[role=option]")
            start = time.monotonic()
            saw_any = False
            while True:
                try:
                    texts = [(options.nth(i).inner_text() or "").strip()
                             for i in range(options.count())]
                except PlaywrightError:
                    texts = []
                if texts:
                    saw_any = True
                    # Settled only once a row that is NOT a loading placeholder
                    # is present -- see _LOADING_OPTION_RE.
                    if any(tx and not _LOADING_OPTION_RE.search(tx) for tx in texts):
                        break
                elapsed = time.monotonic() - start
                if not saw_any and elapsed >= COMBOBOX_FIRST_OPTION_MS / 1000:
                    break       # no sign of a list at all: never suggests
                if elapsed >= COMBOBOX_OPTION_TIMEOUT_MS / 1000:
                    break
                page.wait_for_timeout(200)

            if not saw_any:
                # Advertises role="combobox" but never suggests -- some boards
                # mark a plain input that way. The value is already written, so
                # simply confirm it survives losing focus.
                return AshbyAdapter._value_survives_blur(page, box, value)
            for i in range(options.count()):
                if (options.nth(i).inner_text() or "").strip() == value:
                    options.nth(i).click()
                    return AshbyAdapter._value_survives_blur(page, box, value)

            # No exact match: discard the uncommitted query rather than leave
            # it to read back as a filled value. The live list offered
            # 'Bucharest, Romania' twice alongside 'Romania', so picking the
            # first row blindly is not good enough.
            try:
                box.fill("")
                box.evaluate("el => el.blur()")
            except PlaywrightError:
                pass
            return False
        except PlaywrightError:
            return False

    @staticmethod
    def _value_survives_blur(page, box, value: str) -> bool:
        """True iff `box` still holds `value` after it loses focus.

        Read back only AFTER giving up focus: an uncommitted combobox value
        disappears at exactly that moment, so checking while the field is
        still focused proves nothing. Never raises."""
        try:
            box.evaluate("el => el.blur()")
            page.wait_for_timeout(150)
        except PlaywrightError:
            pass
        try:
            return (box.input_value() or "").strip() == value
        except PlaywrightError:
            return False

    @staticmethod
    def _toggle(wrapper, value: str) -> bool:
        """Press the Yes/No option whose label is `value`, then read it back.

        Verified by TWO independent signals, because a click that silently
        does nothing is exactly how an incomplete application reached the
        submit button on 2026-09-16: the pressed button must report
        aria-pressed="true", AND the widget's backing hidden checkbox must
        AGREE with the answer -- checked for Yes, unchecked for No (it tracks
        Yes; see the return). Measured live on the robco posting:

            before  Yes:aria-pressed=false  No:aria-pressed=false  checkbox=False
            after   Yes:aria-pressed=true   No:aria-pressed=false  checkbox=True

        Returns False on any error, so a required question parks rather than
        being submitted blank.
        """
        try:
            yesno = wrapper.locator(".ashby-application-form-input-yesno").first
            buttons = yesno.locator("button")
            target = None
            for i in range(buttons.count()):
                if (buttons.nth(i).inner_text() or "").strip() == value:
                    target = buttons.nth(i)
                    break
            if target is None:
                return False
            target.click()
            if (target.get_attribute("aria-pressed") or "") != "true":
                return False
            backing = yesno.locator("input[type=checkbox]")
            if backing.count() == 0:
                return False
            # The backing checkbox tracks YES, not "answered". Measured live
            # on andercore 2026-09-24: after No -> checkbox=False, after Yes ->
            # checkbox=True. Requiring it CHECKED failed every "No" -- every
            # visa-sponsorship question, for him -- while the click had
            # worked (No:aria-pressed="true"). aria-pressed above is what
            # proves the click; this proves the widget agrees on WHICH answer.
            return backing.first.is_checked() == (value.strip().lower() == "yes")
        except PlaywrightError:
            return False

    @staticmethod
    def _radio_label(wrapper, radio) -> str:
        """The visible text of one radio option, via its <label for>."""
        try:
            rid = radio.get_attribute("id") or ""
            if not rid:
                return ""
            lab = wrapper.locator(f'label[for="{rid}"]')
            return (lab.first.inner_text() or "").strip() if lab.count() else ""
        except PlaywrightError:
            return ""

    @staticmethod
    def _radio(wrapper, value: str) -> bool:
        """Select the radio option labelled `value`, then read it back.

        Clicks the LABEL, the way a person does: Ashby styles its radios, so
        the input itself is often not actionable. Verified by is_checked() on
        the input, so a click that silently misses parks the question rather
        than leaving it unanswered on submit. Returns False on any error."""
        try:
            radios = wrapper.locator("input[type=radio]")
            for i in range(radios.count()):
                radio = radios.nth(i)
                if AshbyAdapter._radio_label(wrapper, radio) != value:
                    continue
                rid = radio.get_attribute("id") or ""
                wrapper.locator(f'label[for="{rid}"]').first.click()
                return radio.is_checked()
            return False
        except PlaywrightError:
            return False

    @staticmethod
    def _select(page, selector: str, value: str) -> bool:
        """Select the option whose visible label is `value`, then read the
        selection back. Returns False on any error or a mismatch."""
        try:
            page.locator(selector).select_option(label=value)
        except PlaywrightError:
            return False
        return verify_filled(page, selector, value)

    @staticmethod
    def _is_required(label_el, control) -> bool:
        """True when Ashby marks this question required.

        Ashby does NOT use the HTML `required` attribute, and does not set
        aria-required either. Measured live on the robco posting 2026-09-16,
        across all 13 questions: `required` and `aria-required` were None
        everywhere, and no label's inner_text contained an asterisk -- while
        the page plainly rendered red asterisks on six of them.

        The marker is on the LABEL, two ways:
          * a CSS-module class carrying a `_required_` token
            (`_heading_f7cvd_52 _required_f7cvd_91 ...` on a required label,
            the same list MINUS that token on an optional one); and
          * `::after` pseudo-content of "*", which is what actually renders
            and never appears in inner_text.

        Both are checked because the class hash churns between Ashby deploys
        while the rendered asterisk does not. The HTML attribute stays first
        so other boards (and the fixtures) keep working.

        Never raises: an unreadable signal reads as "not required", which is
        the pre-existing behaviour.
        """
        try:
            if control is not None and control.get_attribute("required") is not None:
                return True
        except PlaywrightError:
            pass
        if label_el is None:
            return False
        try:
            if label_el.count() == 0:
                return False
        except PlaywrightError:
            return False
        try:
            classes = label_el.get_attribute("class") or ""
            if "_required_" in classes:
                return True
        except PlaywrightError:
            pass
        try:
            rendered = label_el.evaluate(
                "l => getComputedStyle(l, '::after').content") or ""
            return "*" in rendered
        except PlaywrightError:
            return False

    def _question_for_wrapper(self, wrapper, field_id: str):
        """Return (Question, css_selector, options) for one field-entry
        wrapper, or (None, None, None) when the wrapper's shape isn't a
        fillable question (never raises)."""
        # Ashby question ids are UUIDs; "#<uuid>" is an invalid CSS selector
        # whenever one starts with a digit, which raises rather than missing.
        selector = id_selector(field_id)
        try:
            label_el = wrapper.locator("label").first
            label = label_el.inner_text().strip() if label_el.count() > 0 else field_id

            select_el = wrapper.locator("select")
            if select_el.count() > 0:
                required = self._is_required(label_el, select_el.first)
                options = tuple(
                    opt.inner_text().strip()
                    for opt in select_el.first.locator("option").all()
                    if (opt.get_attribute("value") or "") != ""
                )
                return Question(label=label, kind="select", required=required, options=options), selector, options

            # Yes/No toggle BEFORE the input branch: the widget is backed by a
            # hidden checkbox, so the input branch would otherwise claim it as
            # kind="text" and fill_field would write nothing at all. Measured
            # live on the robco posting 2026-09-16 -- two <button>s inside
            # .ashby-application-form-input-yesno, not a <select>, not a radio
            # group. The container class is the unhashed, stable part.
            yesno_el = wrapper.locator(".ashby-application-form-input-yesno")
            if yesno_el.count() > 0:
                buttons = yesno_el.first.locator("button")
                options = tuple(
                    (buttons.nth(i).inner_text() or "").strip()
                    for i in range(buttons.count())
                    if (buttons.nth(i).inner_text() or "").strip()
                )
                if options:
                    required = self._is_required(label_el, None)
                    return (Question(label=label, kind="radio", required=required,
                                     options=options), selector, options)

            # A GENUINE radio group (input[type=radio] + one <label for> per
            # option), as opposed to the Yes/No button widget above. Live
            # Sardine (score 8) parked unwritable-required on "How did you hear
            # about Sardine?" with the right answer in hand: this shape used to
            # fall through to the text branch and target [id="<field uuid>"],
            # while each radio's id is "<field>_<option>-labeled-radio-N".
            radios = wrapper.locator("input[type=radio]")
            if radios.count() > 0:
                options = tuple(o for o in (self._radio_label(wrapper, radios.nth(i))
                                            for i in range(radios.count())) if o)
                if options:
                    required = self._is_required(label_el, radios.first)
                    return (Question(label=label, kind="radio", required=required,
                                     options=options), selector, options)

            # No element carries the question's id -- live ElevenLabs'
            # "Location" input had no id and no name, and its <label for>
            # pointed at an id nothing on the page has, so every write went to
            # an empty selector. Fall back to the control inside the wrapper,
            # the same remedy _contact_selector applies to _systemfield_location.
            def _scoped(tag: str) -> str:
                if wrapper.locator(selector).count() > 0:
                    return selector
                return f'[data-field-path="{field_id}"] {tag}'

            textarea_el = wrapper.locator("textarea")
            if textarea_el.count() > 0:
                required = self._is_required(label_el, textarea_el.first)
                return (Question(label=label, kind="textarea", required=required),
                        _scoped("textarea"), ())

            input_el = wrapper.locator("input")
            if input_el.count() > 0:
                required = self._is_required(label_el, input_el.first)
                # An <input type="number"> silently REFUSES non-numeric text:
                # fill() writes nothing and verify_filled then fails, so
                # reporting it as kind="text" made screening compose "4321 EUR
                # gross per month" for a box that can only hold 4321, and the
                # run aborted unwritable-required. Measured live on everfield
                # 2026-09-16 (type="number", not readonly, not disabled).
                # screening already understands kind="number"; only this
                # detection was missing.
                input_type = (input_el.first.get_attribute("type") or "").strip().lower()
                kind = "number" if input_type == "number" else "text"
                return Question(label=label, kind=kind, required=required), _scoped("input"), ()
        except PlaywrightError:
            pass
        return None, None, None

    # --- submission -----------------------------------------------------------

    def _await_handoff_submission(self, page, entry: dict, evidence_dir: Path, notify) -> PortalResult:
        """Handoff mode: never click submit -- the human does that themselves
        after solving any CAPTCHA. Notify once, snapshot the same pre-click
        state _submit_and_await_confirmation itself snapshots (initial_url,
        form_present_pre_click -- taken fresh here since nothing has been
        clicked yet), then poll the SAME confirmation signal (_confirmed)
        every 2s up to the handoff timeout. Confirmed -> "submitted" with
        evidence. Timeout -> needs_human, form left exactly as the human
        last saw it (never retried, matching every other no-confirmation
        degrade in this adapter)."""
        company = (entry or {}).get("company", "")
        if notify is not None:
            try:
                notify(f"{company} form filled and waiting: solve any captcha and click submit")
            except Exception:  # noqa: BLE001 -- notify is best-effort
                pass

        try:
            initial_url = page.url
        except PlaywrightError:
            initial_url = None
        form_present_pre_click = self._form_present(page)

        deadline = time.monotonic() + handoff_timeout_s()
        while True:
            if self._confirmed(page, initial_url, form_present_pre_click):
                capture_evidence(page, evidence_dir, "submitted")
                return PortalResult(status="submitted", reason="", evidence_dir=str(evidence_dir))
            if time.monotonic() >= deadline:
                break
            try:
                page.wait_for_timeout(2000)
            except PlaywrightError:
                break

        capture_evidence(page, evidence_dir, "handoff-timeout")
        return PortalResult(status="needs_human",
                             reason="handoff-timeout: not submitted, form left as-is",
                             evidence_dir=str(evidence_dir))

    def _submit_and_await_confirmation(self, page, evidence_dir: Path) -> PortalResult:
        try:
            initial_url = page.url
        except PlaywrightError:
            initial_url = None

        # Snapshot whether #application-form is present using the EXACT same
        # locator _confirmed's signal 3 reads post-click, taken BEFORE the
        # submit click. That id was copied from one real posting's DOM and
        # was never verified against every Ashby board config -- if a real
        # posting's form actually carries a different id, this locator
        # already reads 0 right now, before anything happened. Passing that
        # down means signal 3 is disabled for the whole run whenever the
        # form was never found to begin with, instead of misreading "id
        # never matched" as "form vanished because the submit succeeded".
        form_present_pre_click = self._form_present(page)

        try:
            page.locator(_SUBMIT_SELECTOR).first.click()
        except PlaywrightError as exc:
            capture_evidence(page, evidence_dir, "submit-failed")
            return PortalResult(status="failed", reason=f"submit-click: {exc}", evidence_dir=str(evidence_dir))

        # Let any navigation the submit kicked off settle before we read
        # signals: a plain form GET reload momentarily tears down the form, and
        # reading during that transient would misfire the form-disappeared
        # signal. wait_for_load_state returns immediately for a JS-only submit
        # (page already "load"), and rides out a real navigation otherwise.
        try:
            page.wait_for_load_state("load", timeout=5_000)
        except PlaywrightError:
            pass

        # Poll for ANY confirmation signal up to the cap. On no signal within
        # the cap the reason string tells the human the submit may still have
        # landed -- we never retry a submit that might already have gone
        # through.
        deadline = time.monotonic() + CONFIRMATION_TIMEOUT_MS / 1000
        while True:
            if self._confirmed(page, initial_url, form_present_pre_click):
                capture_evidence(page, evidence_dir, "submitted")
                return PortalResult(status="submitted", reason="", evidence_dir=str(evidence_dir))
            if time.monotonic() >= deadline:
                break
            try:
                page.wait_for_timeout(200)
            except PlaywrightError:
                break

        # An explicit refusal proves nothing was sent, so classify it as such
        # and let apply_policy roll the pre-inserted ledger row back instead of
        # leaving a phantom row that blocks the company forever. Checked only
        # AFTER the confirmation poll has run out, so a page carrying both a
        # success phrase and some unrelated warning still reads as submitted.
        if submit_rejected(page):
            capture_evidence(page, evidence_dir, "submit-rejected")
            return PortalResult(status="needs_human", reason=SUBMIT_REJECTED_REASON,
                                 evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "no-confirmation")
        return PortalResult(status="needs_human", reason=_NO_CONFIRMATION_REASON,
                             evidence_dir=str(evidence_dir))

    @staticmethod
    def _note_target(page):
        """A VISIBLE, meaningfully-labelled long-form box to carry a note that
        the answer's own control cannot hold, or None.

        The cover letter is preferred: it is a letter to the employer, stating
        a salary expectation in it is normal, and the adapter has already
        written the letter there (so _append_note must not clobber it).

        Deliberately strict about the fallback. Live everfield 2026-09-16 has
        NOWHERE to say this: its "Cover letter" is a FILE upload and its only
        <textarea> is reCAPTCHA's hidden, unlabelled g-recaptcha-response --
        the same field that polluted form_state.json. A stray text input is
        not acceptable either; everfield's other one asks for a GitHub link,
        and a salary sentence does not belong there. None means PARK."""
        try:
            # MUST be a textarea, not merely that id: live everfield's cover
            # letter is a FILE upload, and matching the id alone returned that
            # input -- visible, so no park fired, the number was typed, and
            # only then did the append fail. The bare figure would have been
            # written with the currency stated nowhere.
            letter = page.locator(f"textarea{_COVER_LETTER_SELECTOR}")
            if letter.count() > 0 and letter.first.is_visible():
                return letter.first
            boxes = page.locator("[data-field-path] textarea")
            for i in range(boxes.count()):
                box = boxes.nth(i)
                name = (box.get_attribute("name") or "").lower()
                if "captcha" in name:
                    continue
                if not box.is_visible():
                    continue
                wrapper = box.locator("xpath=ancestor::*[@data-field-path][1]")
                if not (wrapper.inner_text() or "").strip():
                    continue  # unlabelled: not somewhere a human would read
                return box
        except PlaywrightError:
            return None
        return None

    @staticmethod
    def _append_note(box, note: str) -> bool:
        """Add `note` to a free-text box without destroying what is there."""
        try:
            existing = (box.input_value() or "").strip()
            box.fill(f"{existing}\n\n{note}" if existing else note)
            return note in (box.input_value() or "")
        except PlaywrightError:
            return False

    @staticmethod
    def _form_present(page) -> bool:
        """True if #application-form currently resolves to a visible element.
        Used both to snapshot pre-click state (see _submit_and_await_confirmation)
        and, transitively, as the gate for _confirmed's signal 3. Never
        raises -- a locator error just reads as "not present"."""
        try:
            form = page.locator("#application-form")
            return form.count() > 0 and form.first.is_visible()
        except PlaywrightError:
            return False

    @staticmethod
    def _confirmed(page, initial_url, form_present_pre_click: bool) -> bool:
        """True if any confirmation signal is present:
          1. a visible confirmation phrase;
          2. a navigation to a genuinely different PATH (a query-only change --
             e.g. a plain form GET reload back to the same page -- is NOT a
             success signal);
          3. the application form having disappeared with no error banner AND
             the URL entirely unchanged, i.e. an SPA that swapped the form out
             for a success view without navigating (gating on the unchanged URL
             keeps a GET-reload transient from being misread as this). This
             signal is only meaningful when the form was actually PRESENT
             before the submit click (form_present_pre_click) -- if the
             #application-form locator never matched to begin with (a real
             posting whose form carries a different id, never verified against
             live Ashby DOM), "form_gone" is trivially true on the very first
             poll and would misread a submit that never even reached a valid
             submit button as already submitted."""
        try:
            current_url = page.url
        except PlaywrightError:
            current_url = None

        # 1) A visible confirmation phrase.
        try:
            phrase = page.get_by_text(_CONFIRMATION_RE)
            if phrase.count() > 0 and phrase.first.is_visible():
                return True
        except PlaywrightError:
            pass
        # 2) Navigation to a genuinely different path.
        if initial_url is not None and current_url is not None:
            try:
                if urlparse(current_url).path != urlparse(initial_url).path:
                    return True
            except ValueError:
                pass
        # 3) SPA form-vanish with no navigation and no error banner -- disabled
        # entirely when the form was never confirmed present pre-click.
        if form_present_pre_click and current_url == initial_url:
            try:
                form = page.locator("#application-form")
                form_gone = form.count() == 0 or not form.first.is_visible()
                has_error = page.locator(_ERROR_BANNER_SELECTOR).count() > 0
                if form_gone and not has_error:
                    return True
            except PlaywrightError:
                pass
        return False


ASHBY_ADAPTER = register_adapter(AshbyAdapter())
