# src/cv_tailor/portal/micro1.py
"""Micro1 (jobs.micro1.ai) portal adapter.

A Micro1 posting (`jobs.micro1.ai/post/<uuid>`) is a Next.js SPA whose
"Interested?" application card lives directly on the posting page itself --
no separate Application tab (Ashby) or `/apply` route (Lever) to navigate to
first, same shape as Greenhouse. Field names/classes below are copied
faithfully from a real posting fetched live while building this adapter
(jobs.micro1.ai/post/c368cc32-d267-490e-abed-e9521cdf628e, "Content
Producer"; see tests/fixtures/portal/micro1_form.html for the provenance
note and documented simplifications):

    input[name=first_name] / input[name=last_name] / input[name=email_id]
    a react-phone-number-input widget: select.PhoneInputCountrySelect
        (aria-label "Phone number country", NO name attribute) + a sibling
        input.PhoneInputInput (type=tel, also NO name attribute)
    input[name=linkedin_url]
    input[type=file][id=file][name=file] (hidden behind a styled dropzone
        <label for=file>, accept=".pdf")
    button[type=submit] labeled "Next"

Resume upload, live-verified (see this module's git history / build notes,
and CV-1092-style report at .superpowers/sdd/scout-c/micro1-report.md):
`set_input_files()` on the file input flips `input.files` SYNCHRONOUSLY, but
the dropzone widget's own React state -- the thing the Next-click validator
actually gates on ("Resume is required") -- updates roughly 1.4s later
(presumably a resume-parse round trip), replacing its "Click to upload or
drag & drop (.pdf)" placeholder text with the filename. Verifying by
`input.files` alone (four real armed runs did exactly this and all four
failed identically) reports success while the widget still shows an empty
dropzone and Next rejects the submission. `_upload_and_verify_resume`
therefore polls the dropzone's own visible text for the placeholder to be
replaced, bounded by APPLY_RESUME_WIDGET_TIMEOUT_MS (default 8000ms, well
above the observed 1.4s).

Phone field, live-verified (see this module's git history / build notes):
the posting geo-defaults the country select to Romania and pre-fills the
tel input with just the dial code ("+40"). `.fill()`-ing that input with
DIGITS ONLY appends onto the existing "+40" and the widget reformats to
"+40 725 697 859" -- exactly right. `.fill()`-ing it with a FULL number
that includes a *different* country's dial code (e.g. while "United
States" is selected) is silently REJECTED by the widget -- the value
does not change at all. So the safe write is: force the country select to
Romania first (only if it doesn't already show it), then fill just the
local digits (`_local_phone_digits` strips a leading "40" off
profile.contact.phone, which is always a Romanian number for this
adapter's one user). Verification reads the field back and compares
digits-only, ignoring the widget's own space-formatting.

The "Next" click only ever submits the FIRST step (contact info + resume).
The posting's own copy warns of "the interview process" afterward, so a
second step of extra questions is a real possibility this adapter must
handle without ever guessing an answer. Unlike Ashby/Greenhouse/Lever --
where handoff mode never clicks submit itself, a human always does --
Micro1's initial "Next" is low-stakes enough that BOTH armed and handoff
modes click it themselves; the human/handoff boundary here is instead
"can a second step of questions be answered safely", not "can the click be
made at all". Flow after dry_run (which never clicks anything):

    click Next -> blocker recheck -> confirmation phrase present -> submitted
                                   -> new `[data-micro1-question]` fields
                                      present but nothing confirmed yet:
                                        handoff -> notify + poll for a human
                                                   to finish it (blocker-wait
                                                   shape), never auto-answers
                                        armed   -> answer_question each field
                                                   (unanswerable-required ->
                                                   needs_human), click Next
                                                   again -- capped at
                                                   _MAX_STEPS total clicks
                                   -> neither signal -> ambiguous, degrades
                                      to the standard no-confirmation
                                      needs_human after _MAX_STEPS clicks
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError

from cv_tailor.portal.base import (
    PortalAdapter,
    PortalResult,
    capture_evidence,
    detect_blockers,
    fill_field,
    handoff_timeout_s,
    register_adapter,
    resolve_blocker,
    verify_file_attached,
    verify_filled,
)
from cv_tailor.screening import Question, answer_question

_FIRST_NAME_SELECTOR = "input[name='first_name']"
_LAST_NAME_SELECTOR = "input[name='last_name']"
_EMAIL_SELECTOR = "input[name='email_id']"
_LINKEDIN_SELECTOR = "input[name='linkedin_url']"
_PHONE_COUNTRY_SELECT_SELECTOR = "select.PhoneInputCountrySelect"
_PHONE_TEL_SELECTOR = "input.PhoneInputInput"
_RESUME_SELECTOR = "input[type='file']"
_NEXT_SELECTOR = "button:has-text('Next')"
_QUESTION_WRAPPER_SELECTOR = "[data-micro1-question]"
_ERROR_BANNER_SELECTOR = "[role='alert'], .error, .form-error, [aria-invalid='true']"

_ROMANIA_OPTION_LABEL = "Romania"
_ROMANIA_DIAL_CODE = "40"
_PHONE_DIGITS_RE = re.compile(r"\D")

# Substring of the dropzone's own placeholder copy ("Click to upload or
# drag & drop (.pdf)"). Its disappearance -- not input.files -- is the real
# signal that the widget registered the upload; see the module docstring.
_RESUME_PLACEHOLDER_SNIPPET = "Click to upload"


def _resume_widget_settle_timeout_ms() -> float:
    """Bound for polling the resume dropzone's own UI state after
    set_input_files (see _wait_for_resume_widget_state). Overridable via
    APPLY_RESUME_WIDGET_TIMEOUT_MS so tests can prove the negative case (the
    widget never updates) without a real multi-second wait every run. The
    8000ms default sits well above the ~1.4s settle observed live against
    the real posting."""
    return float(os.environ.get("APPLY_RESUME_WIDGET_TIMEOUT_MS", "8000"))

# Never click Next/submit more than this many times in one run: the first
# click (contact+resume step) plus at most two more answered-question steps.
# A real posting stuck asking for a 4th round would be unusual enough to
# warrant a human look rather than an unbounded auto-click loop.
_MAX_STEPS = 3

_CONFIRMATION_RE = re.compile(
    r"application (?:received|submitted)|thank you for applying|thanks for applying|"
    r"successfully submitted|been submitted|application complete|we'll be in touch",
    re.I,
)
_NO_CONFIRMATION_REASON = (
    "no-confirmation: submission may have succeeded, VERIFY on the portal "
    "before applying manually"
)


def _first_name(full_name: str) -> str:
    return (full_name or "").strip().split(" ", 1)[0]


def _last_name(full_name: str) -> str:
    parts = (full_name or "").strip().split(" ", 1)
    return parts[1] if len(parts) > 1 else ""


def _normalize_linkedin(url: str) -> str:
    url = (url or "").strip()
    if not url or url.lower().startswith(("http://", "https://")):
        return url
    return f"https://{url}"


def _local_phone_digits(phone: str) -> str:
    """Digits-only local number for the tel input, assuming Romania is (or
    will be) the selected country. Strips all non-digit characters, then a
    single leading "40" (Romania's dial code) if present -- profile.contact
    .phone is stored as "+40 725 697 859", and the widget already shows the
    "+40" prefix once Romania is selected, so re-sending it would duplicate
    the code. A number with no leading "40" is returned as-is (best effort;
    this adapter has exactly one user, whose number is always Romanian)."""
    digits = _PHONE_DIGITS_RE.sub("", phone or "")
    if digits.startswith(_ROMANIA_DIAL_CODE) and len(digits) > len(_ROMANIA_DIAL_CODE):
        return digits[len(_ROMANIA_DIAL_CODE):]
    return digits


class Micro1Adapter(PortalAdapter):
    hosts = ("jobs.micro1.ai",)
    name = "micro1"

    def apply(self, page, entry: dict, package: dict, profile: dict,
              answers: dict, *, dry_run: bool, client: Any = None,
              deployment: str | None = None, handoff: bool = False,
              notify: Any = None) -> PortalResult:
        evidence_dir = Path(package["package_dir"]) / "portal"

        # The posting's data (skills, pay, description) streams in after
        # "load"; a short networkidle settle avoids typing into a React
        # controlled input before its onChange handler is attached (same
        # rationale as Greenhouse's own settle wait). A static fixture page
        # has nothing left in flight so this returns immediately there.
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightError:
            pass

        blocker = detect_blockers(page)
        if blocker:
            result = resolve_blocker(page, blocker, evidence_dir, stage=blocker,
                                      handoff=handoff, notify=notify)
            if result is not None:
                return result

        contact = (profile or {}).get("contact", {}) or {}
        fill_field(page, _FIRST_NAME_SELECTOR, _first_name(contact.get("name", "")))
        fill_field(page, _LAST_NAME_SELECTOR, _last_name(contact.get("name", "")))
        fill_field(page, _EMAIL_SELECTOR, contact.get("email", ""))
        fill_field(page, _LINKEDIN_SELECTOR, _normalize_linkedin(contact.get("linkedin", "")))
        self._fill_phone(page, contact.get("phone", ""))

        # Write-verify the fields that matter most: a grounded value that
        # never reached the DOM must abort, not submit a form missing (or
        # misrepresenting) the applicant's identity.
        if not self._verify_contact(page, contact):
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason="contact-fill-failed",
                                 evidence_dir=str(evidence_dir))

        uploaded, upload_detail = self._upload_and_verify_resume(page, package.get("cv_path"))
        if not uploaded:
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason=f"resume-upload-failed: {upload_detail}",
                                 evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "filled")

        if dry_run:
            return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))

        return self._submit_flow(page, entry, evidence_dir, profile, answers,
                                  client=client, deployment=deployment,
                                  handoff=handoff, notify=notify)

    # --- phone -----------------------------------------------------------------

    def _fill_phone(self, page, phone: str) -> bool:
        if not phone:
            return False
        select = page.locator(_PHONE_COUNTRY_SELECT_SELECTOR)
        if select.count() > 0:
            try:
                current = select.first.input_value()
            except PlaywrightError:
                current = ""
            if current != "RO":
                try:
                    select.first.select_option(label=_ROMANIA_OPTION_LABEL)
                except PlaywrightError:
                    pass
        local = _local_phone_digits(phone)
        if not local:
            return False
        return fill_field(page, _PHONE_TEL_SELECTOR, local)

    @staticmethod
    def _verify_phone(page, phone: str) -> bool:
        expected_digits = _local_phone_digits(phone)
        if not expected_digits:
            return False
        try:
            actual = page.locator(_PHONE_TEL_SELECTOR).first.input_value()
        except PlaywrightError:
            return False
        return _PHONE_DIGITS_RE.sub("", actual).endswith(expected_digits)

    def _verify_contact(self, page, contact: dict) -> bool:
        first = _first_name(contact.get("name", ""))
        if first and not verify_filled(page, _FIRST_NAME_SELECTOR, first):
            return False
        email = contact.get("email", "")
        if email and not verify_filled(page, _EMAIL_SELECTOR, email):
            return False
        phone = contact.get("phone", "")
        if phone and not self._verify_phone(page, phone):
            return False
        return True

    # --- resume ------------------------------------------------------------------

    @staticmethod
    def _upload_and_verify_resume(page, cv_path) -> tuple[bool, str]:
        if not cv_path:
            return False, "no cv_path provided"
        # Re-query rather than reuse any earlier handle: a SPA remount
        # between this adapter's top-level networkidle settle and here
        # would otherwise leave the locator pointed at a detached node.
        locator = page.locator(_RESUME_SELECTOR)
        if locator.count() == 0:
            return False, f"no file input found ({_RESUME_SELECTOR} matched 0 elements)"
        try:
            locator.first.set_input_files(str(cv_path))
        except PlaywrightError as exc:
            return False, f"set_input_files raised {type(exc).__name__}: {exc}"

        if not verify_file_attached(page, _RESUME_SELECTOR):
            return False, (
                "set_input_files ran against a matched file input but files.length "
                "was 0 after upload"
            )

        # input.files is necessary but NOT sufficient: live-verified against
        # the real posting, it flips synchronously while the dropzone
        # widget's own React state -- what the Next-click validator actually
        # gates on -- updates ~1.4s later. Four real armed runs trusted
        # files.length alone and all four got rejected with "Resume is
        # required". Poll the widget's own visible text instead.
        timeout_ms = _resume_widget_settle_timeout_ms()
        if Micro1Adapter._wait_for_resume_widget_state(page, _RESUME_SELECTOR, timeout_ms):
            return True, ""
        return False, (
            "input.files shows the file attached but the dropzone widget never "
            f"left its \"{_RESUME_PLACEHOLDER_SNIPPET}...\" placeholder state "
            f"within {timeout_ms:.0f}ms -- verify-by-input.files is not proof "
            "the widget (and the Next-click validator gating on it) registered "
            "the upload"
        )

    @staticmethod
    def _wait_for_resume_widget_state(page, selector: str, timeout_ms: float) -> bool:
        """Poll the resume dropzone's own visible text (its parent element's
        innerText) for the placeholder copy to be replaced by an
        uploaded-file indicator (filename chip / remove control) -- the only
        signal the real widget's Next-click validator actually trusts.
        Bounded by timeout_ms; a timeout or any Playwright error is a plain
        False, never a raise."""
        selector_js = json.dumps(selector)
        placeholder_js = json.dumps(_RESUME_PLACEHOLDER_SNIPPET)
        try:
            page.wait_for_function(
                f"""() => {{
                    const el = document.querySelector({selector_js});
                    if (!el || !el.parentElement) return false;
                    const text = (el.parentElement.innerText || "").trim();
                    return text.length > 0 && !text.includes({placeholder_js});
                }}""",
                timeout=timeout_ms,
            )
            return True
        except PlaywrightError:
            return False

    # --- submission ----------------------------------------------------------------

    def _submit_flow(self, page, entry: dict, evidence_dir: Path, profile: dict,
                      answers: dict, *, client, deployment, handoff, notify) -> PortalResult:
        company = (entry or {}).get("company", "")
        steps = 0
        while steps < _MAX_STEPS:
            steps += 1
            try:
                page.locator(_NEXT_SELECTOR).first.click()
            except PlaywrightError as exc:
                capture_evidence(page, evidence_dir, "submit-failed")
                return PortalResult(status="failed", reason=f"submit-click: {exc}",
                                     evidence_dir=str(evidence_dir))

            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightError:
                pass

            blocker = detect_blockers(page)
            if blocker:
                result = resolve_blocker(page, blocker, evidence_dir, stage=blocker,
                                          handoff=handoff, notify=notify)
                if result is not None:
                    return result

            if self._confirmed(page):
                capture_evidence(page, evidence_dir, "submitted")
                return PortalResult(status="submitted", reason="", evidence_dir=str(evidence_dir))

            questions = self._discover_step_questions(page)
            if not questions:
                break  # neither confirmed nor more to answer -- ambiguous

            if handoff:
                return self._await_handoff_step(page, company, evidence_dir, notify)

            aborted = self._answer_step_questions(page, questions, profile, answers,
                                                   evidence_dir, client=client, deployment=deployment)
            if aborted is not None:
                return aborted
            # loop: click Next/submit again on the now-answered step, bounded
            # by _MAX_STEPS above.

        capture_evidence(page, evidence_dir, "no-confirmation")
        return PortalResult(status="needs_human", reason=_NO_CONFIRMATION_REASON,
                             evidence_dir=str(evidence_dir))

    def _await_handoff_step(self, page, company: str, evidence_dir: Path, notify) -> PortalResult:
        """A later step asked something this adapter won't guess at even
        while armed -- in handoff mode a human finishes it themselves.
        Notify once, then poll the same confirmation signal every 2s up to
        the handoff timeout, exactly like resolve_blocker's own wait shape.
        Never fills or clicks anything itself from here on."""
        if notify is not None:
            try:
                notify(f"{company} micro1 form needs more info: waiting for a human to continue")
            except Exception:  # noqa: BLE001 -- notify is best-effort
                pass

        deadline = time.monotonic() + handoff_timeout_s()
        while True:
            if self._confirmed(page):
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

    @staticmethod
    def _confirmed(page) -> bool:
        """A visible confirmation/thank-you phrase only -- NOT "the first
        step's fields are gone", which is ambiguous here (a second step of
        questions legitimately replaces the first step's DOM too)."""
        try:
            phrase = page.get_by_text(_CONFIRMATION_RE)
            return phrase.count() > 0 and phrase.first.is_visible()
        except PlaywrightError:
            return False

    # --- second-step screening questions --------------------------------------------

    def _discover_step_questions(self, page) -> list[tuple[str, Question]]:
        try:
            wrappers = page.locator(_QUESTION_WRAPPER_SELECTOR).all()
        except PlaywrightError:
            return []
        out: list[tuple[str, Question]] = []
        for wrapper in wrappers:
            field_id = wrapper.get_attribute("data-micro1-question") or ""
            if not field_id:
                continue
            question, selector = self._question_for_wrapper(wrapper, field_id)
            if question is not None:
                out.append((selector, question))
        return out

    @staticmethod
    def _question_for_wrapper(wrapper, field_id: str):
        base_selector = f"[data-micro1-question='{field_id}']"
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
                return (Question(label=label, kind="select", required=required, options=options),
                        f"{base_selector} select")

            textarea_el = wrapper.locator("textarea")
            if textarea_el.count() > 0:
                required = textarea_el.first.get_attribute("required") is not None
                return Question(label=label, kind="textarea", required=required), f"{base_selector} textarea"

            input_el = wrapper.locator("input")
            if input_el.count() > 0:
                required = input_el.first.get_attribute("required") is not None
                return Question(label=label, kind="text", required=required), f"{base_selector} input"
        except PlaywrightError:
            pass
        return None, None

    def _answer_step_questions(self, page, questions, profile: dict, answers: dict,
                                evidence_dir: Path, *, client, deployment) -> PortalResult | None:
        for selector, question in questions:
            answer = answer_question(question, profile, answers, client=client, deployment=deployment)
            if answer is None or not answer.value:
                if question.required:
                    capture_evidence(page, evidence_dir, "aborted")
                    return PortalResult(status="needs_human",
                                         reason=f"unanswerable-required:{question.label}",
                                         evidence_dir=str(evidence_dir))
                continue

            if question.kind == "select":
                written = self._select(page, selector, answer.value)
            else:
                written = fill_field(page, selector, answer.value) and \
                    verify_filled(page, selector, answer.value)

            if not written and question.required:
                capture_evidence(page, evidence_dir, "aborted")
                return PortalResult(status="needs_human",
                                     reason=f"unwritable-required:{question.label}",
                                     evidence_dir=str(evidence_dir))
        return None

    @staticmethod
    def _select(page, selector: str, value: str) -> bool:
        try:
            page.locator(selector).select_option(label=value)
        except PlaywrightError:
            return False
        return verify_filled(page, selector, value)


MICRO1_ADAPTER = register_adapter(Micro1Adapter())
