# src/cv_tailor/portal/lever.py
"""Lever ATS adapter: fills a Lever application form (jobs.lever.co) and
gates submission behind `dry_run`.

Lever postings live at `jobs.lever.co/<org>/<id>`; the application form is a
separate page at `.../apply`. The posting page carries no `#application-form`
-- only a link to `/apply` -- so `apply()` treats the presence of that form
in the DOM as "already on the apply page" and only navigates when it is
absent. This is more robust than string-matching the URL (which fixture
tests would defeat) and matches Lever's real markup.

Classic Lever fields (`name`, `email`, `phone`, `location`, `urls[LinkedIn]`,
`urls[GitHub]`) are filled directly from `profile.contact` -- the same
grounded source `cv_tailor.screening` reads for its own contact-field
answers. The resume upload and cover-letter textarea (`name="comments"`,
Lever's documented additional-info field) come from the assembled package.
Everything else on the form (custom cards, `org`/"Current company", the EEO
`eeo[gender]`/`eeo[race]`/`eeo[veteran]` selects, and any radio/checkbox
question groups) is discovered generically from the DOM and answered
through `cv_tailor.screening.answer_question` -- nothing here invents an
answer Teodor hasn't actually given.
"""
from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
from cv_tailor.screening import Answer, Question, answer_question

# Fields filled directly from profile.contact / the assembled package --
# never re-discovered as a generic screening Question, even if the ATS also
# marks them "required" (that would ask the LLM to re-derive a fact we
# already have, and risk drifting from the grounded value).
_DIRECT_FILL_NAMES = frozenset({
    "name", "email", "phone", "location", "urls[LinkedIn]", "urls[GitHub]",
    "resume", "comments",
})

_CONFIRMATION_SELECTOR = "text=/application.*(submitted|received)|thank you for applying/i"
_CONFIRMATION_TIMEOUT_MS = 30_000
# Standardized with Ashby's own no-confirmation reason (see ashby.py): what a
# human sees, so it must say the submission MIGHT have gone through -- used
# for BOTH a plain confirmation timeout and any other post-click
# PlaywrightError (see apply()'s submit step, ~C345 Phase C fix).
_NO_CONFIRMATION_REASON = (
    "no-confirmation: submission may have succeeded, VERIFY on the portal "
    "before applying manually"
)


def _clean_label(raw: str) -> str:
    return (raw or "").replace("✱", "").replace("*", "").strip()


def _label_for(block) -> str:
    label = block.locator(".application-label").first
    if label.count() == 0:
        return ""
    try:
        return _clean_label(label.inner_text())
    except PlaywrightError:
        return ""


def _options_for(locators) -> tuple[str, ...]:
    """Option VALUES for a radio/checkbox group (what `.check()` targets)."""
    values = []
    for j in range(locators.count()):
        try:
            values.append((locators.nth(j).get_attribute("value") or "").strip())
        except PlaywrightError:
            continue
    return tuple(v for v in values if v)


def _select_option_texts(select_loc) -> tuple[str, ...]:
    """Visible option TEXT for a <select> (skipping the empty-value
    placeholder). Selects are option-matched by human-readable text so the
    screening tiers (EEO decline detection, yes/no mapping, the LLM option
    gate) reason over what a person reads, not an opaque coded value; the
    adapter maps the chosen text back to the option's value at fill time."""
    opts = select_loc.locator("option")
    texts = []
    for j in range(opts.count()):
        opt = opts.nth(j)
        try:
            if (opt.get_attribute("value") or "").strip() == "":
                continue
            texts.append((opt.inner_text() or "").strip())
        except PlaywrightError:
            continue
    return tuple(t for t in texts if t)


def _distinct_names(locators) -> list[str]:
    """Ordered distinct `name` attributes across a group of inputs."""
    names: list[str] = []
    for j in range(locators.count()):
        try:
            name = locators.nth(j).get_attribute("name") or ""
        except PlaywrightError:
            continue
        if name and name not in names:
            names.append(name)
    return names


def _attr_required(field) -> bool:
    try:
        return field.get_attribute("required") is not None
    except PlaywrightError:
        return False


def discover_questions(page) -> list[tuple[Question, str]]:
    """Walk every `.application-question` block not already covered by the
    direct-fill step and turn EACH named form control in it into a
    (Question, field_name) pair. Lever marks standard/custom-card questions
    with `<li class="application-question">` but EEO questions with
    `<div class="application-question">` -- the class selector (not
    tag-scoped) catches both. A single block may hold MORE THAN ONE named
    control (e.g. a references card with two inputs); every distinct named
    control is enumerated, not just the first. Blocks with no named form
    control (e.g. Lever's "Apply with LinkedIn" autofill widget) yield
    nothing -- there is nothing to answer or fill."""
    out: list[tuple[Question, str]] = []
    blocks = page.locator(".application-question")

    for i in range(blocks.count()):
        block = blocks.nth(i)
        label = _label_for(block)
        block_required = block.locator(".required").count() > 0
        claimed: set[str] = set()

        def _claim(name: str) -> bool:
            if not name or name in _DIRECT_FILL_NAMES or name in claimed:
                return False
            claimed.add(name)
            return True

        # <select> controls
        selects = block.locator("select")
        for s in range(selects.count()):
            sel = selects.nth(s)
            name = sel.get_attribute("name") or ""
            if not _claim(name):
                continue
            options = _select_option_texts(sel)
            required = block_required or _attr_required(sel)
            out.append((Question(label, "select", required, options), name))

        # radio groups
        for name in _distinct_names(block.locator("input[type='radio']")):
            if not _claim(name):
                continue
            group = block.locator(f"input[type='radio'][name='{name}']")
            required = block_required or any(
                _attr_required(group.nth(k)) for k in range(group.count())
            )
            out.append((Question(label, "radio", required, _options_for(group)), name))

        # checkbox groups
        for name in _distinct_names(block.locator("input[type='checkbox']")):
            if not _claim(name):
                continue
            group = block.locator(f"input[type='checkbox'][name='{name}']")
            required = block_required or any(
                _attr_required(group.nth(k)) for k in range(group.count())
            )
            out.append((Question(label, "checkbox", required, _options_for(group)), name))

        # free-text / number / textarea controls (each its own named field)
        fields = block.locator(
            "input[type='text'], input[type='email'], input[type='tel'], "
            "input[type='number'], textarea"
        )
        for f in range(fields.count()):
            fld = fields.nth(f)
            name = fld.get_attribute("name") or ""
            if not _claim(name):
                continue
            try:
                tag = fld.evaluate("el => el.tagName.toLowerCase()")
            except PlaywrightError:
                tag = "input"
            ftype = (fld.get_attribute("type") or "").lower()
            if tag == "textarea":
                kind = "textarea"
            elif ftype == "number":
                kind = "number"
            else:
                kind = "text"
            out.append((Question(label, kind, block_required or _attr_required(fld), ()), name))

    return out


class LeverAdapter(PortalAdapter):
    """Fill (and, when armed, submit) a Lever application form."""

    hosts = ("jobs.lever.co",)
    name = "lever"

    def __init__(self, *, client=None, deployment=None):
        # Constructor client/deployment are a fallback only, kept for the
        # adapter's own direct-construction tests (LeverAdapter(client=...))
        # and for a caller that holds its own instance. The module-level
        # ASHBY/GREENHOUSE/LEVER singletons registered at import time can't
        # be constructed per-call, so the orchestrator's real path is the
        # apply(..., client=..., deployment=...) keyword (unified with the
        # other two adapters) -- see apply() below, which prefers the kwarg
        # over these attributes when both are given.
        self.client = client
        self.deployment = deployment

    @staticmethod
    def _verify_contact(page, contact: dict) -> bool:
        """Read back name + email after filling. Only non-empty grounded values
        are checked. Returns False if either given value did not land."""
        for selector, key in (("input[name='name']", "name"), ("input[name='email']", "email")):
            expected = (contact or {}).get(key, "")
            if expected and not verify_filled(page, selector, expected):
                return False
        return True

    @staticmethod
    def _upload_and_verify_resume(page, cv_path) -> bool:
        """Upload the resume and confirm it actually attached. Returns False on
        a missing cv_path, a selector that matches nothing, an upload error, or
        a file that never landed."""
        if not cv_path:
            return False
        try:
            resume = page.locator("input[name='resume']")
            if resume.count() == 0:
                return False
            resume.first.set_input_files(cv_path)
        except PlaywrightError:
            return False
        return verify_file_attached(page, "input[name='resume']")

    def _answer_all(self, page, evidence_dir, profile, answers, *, client, deployment) -> PortalResult | None:
        """Fill every remaining discovered question and verify each write.
        Returns a needs_human PortalResult if a REQUIRED question can't be
        grounded (unanswerable-required) or was grounded but the value didn't
        land in the DOM (unwritable-required), else None."""
        for question, field_name in discover_questions(page):
            answer = answer_question(question, profile, answers, client=client, deployment=deployment)
            if answer is None:
                capture_evidence(page, evidence_dir, "aborted")
                return PortalResult(status="needs_human",
                                     reason=f"unanswerable-required:{question.label}",
                                     evidence_dir=str(evidence_dir))
            if not answer.value:
                continue
            if not self._apply_answer(page, question, field_name, answer) and question.required:
                capture_evidence(page, evidence_dir, "aborted")
                return PortalResult(status="needs_human",
                                     reason=f"unwritable-required:{question.label}",
                                     evidence_dir=str(evidence_dir))
        return None

    def _apply_answer(self, page, question: Question, field_name: str, answer: Answer) -> bool:
        """Fill one question and verify the write landed. Returns False on any
        failure so the caller can enforce the required policy. Optional-field
        failures are simply reported as False (left best-effort by the caller)."""
        if question.kind == "select":
            return self._select_by_value(page, field_name, answer.value)
        if question.kind in ("radio", "checkbox"):
            return self._check_option(page, question.kind, field_name, answer.value)
        selector = f"[name='{field_name}']"
        if not fill_field(page, selector, answer.value):
            return False
        return verify_filled(page, selector, answer.value)

    @staticmethod
    def _select_by_value(page, field_name: str, answer_value: str) -> bool:
        """Select the option matching `answer_value` (by visible text OR by
        value) and drive it via `select_option(value=...)` -- Lever's rendered
        option text and its submitted value differ on some boards, so choosing
        by value is what actually lands the right answer. Verifies the
        selection landed."""
        try:
            select = page.locator(f"select[name='{field_name}']").first
            if select.count() == 0:
                return False
            opts = select.locator("option")
            target = None
            for j in range(opts.count()):
                opt = opts.nth(j)
                text = (opt.inner_text() or "").strip()
                val = (opt.get_attribute("value") or "").strip()
                if answer_value in (text, val):
                    target = val
                    break
            if target is None:
                return False
            select.select_option(value=target)
        except PlaywrightError:
            return False
        return verify_filled(page, f"select[name='{field_name}']", target)

    @staticmethod
    def _check_option(page, kind: str, field_name: str, value: str) -> bool:
        """Check the radio/checkbox in group `field_name` whose VALUE equals
        `value`, then confirm it is checked. The value is matched in Python
        (never interpolated into a selector), so an option value containing a
        quote/backslash/other CSS metacharacter cannot break the locator."""
        try:
            group = page.locator(f"input[type='{kind}'][name='{field_name}']")
            for j in range(group.count()):
                el = group.nth(j)
                if (el.get_attribute("value") or "") == value:
                    el.check()
                    return bool(el.is_checked())
        except PlaywrightError:
            return False
        return False

    def apply(self, page, entry: dict, package: dict, profile: dict, answers: dict, *,
              dry_run: bool, client=None, deployment=None, handoff: bool = False,
              notify=None) -> PortalResult:
        # The kwarg is the orchestrator's real path (the module-level LEVER
        # singleton is constructed once at import time with client=None, so
        # it can never carry a per-call client via the constructor). Fall
        # back to the constructor attributes only when the kwarg is omitted,
        # so direct-construction tests (LeverAdapter(client=...)) keep working.
        client = client if client is not None else self.client
        deployment = deployment if deployment is not None else self.deployment
        evidence_dir = Path(package["package_dir"]) / "portal"

        blocker = detect_blockers(page)
        if blocker:
            result = resolve_blocker(page, blocker, evidence_dir, stage="blocked",
                                      handoff=handoff, notify=notify)
            if result is not None:
                return result

        if page.locator("#application-form").count() == 0:
            apply_url = page.url.rstrip("/") + "/apply"
            try:
                page.goto(apply_url, wait_until="load")
            except PlaywrightTimeoutError:
                capture_evidence(page, evidence_dir, "timeout")
                return PortalResult(status="needs_human", reason="timeout", evidence_dir=str(evidence_dir))

            blocker = detect_blockers(page)
            if blocker:
                result = resolve_blocker(page, blocker, evidence_dir, stage="blocked",
                                          handoff=handoff, notify=notify)
                if result is not None:
                    return result

        contact = (profile or {}).get("contact", {}) or {}
        fill_field(page, "input[name='name']", contact.get("name", ""))
        fill_field(page, "input[name='email']", contact.get("email", ""))
        fill_field(page, "input[name='phone']", contact.get("phone", ""))
        fill_field(page, "input[name='location']", contact.get("location", ""))
        fill_field(page, "input[name='urls[LinkedIn]']", contact.get("linkedin", ""))
        fill_field(page, "input[name='urls[GitHub]']", contact.get("github", ""))

        # Verify the two universally-required contact fields (name, email)
        # landed -- a grounded value that never reached the DOM must abort, not
        # submit a form missing the applicant's identity.
        if not self._verify_contact(page, contact):
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason="contact-fill-failed",
                                 evidence_dir=str(evidence_dir))

        # The resume upload is write-VERIFIED (el.files length). A missing
        # cv_path, a selector that matches nothing, an upload error, or a file
        # that never attached must abort to needs_human BEFORE any armed submit
        # rather than applying with no resume.
        if not self._upload_and_verify_resume(page, package.get("cv_path")):
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason="resume-upload-failed",
                                 evidence_dir=str(evidence_dir))

        cover_letter_path = package.get("cover_letter_path")
        if cover_letter_path:
            try:
                cover_text = Path(cover_letter_path).read_text()
            except OSError:
                cover_text = ""
            fill_field(page, "textarea[name='comments']", cover_text)

        aborted = self._answer_all(page, evidence_dir, profile, answers, client=client, deployment=deployment)
        if aborted is not None:
            return aborted

        capture_evidence(page, evidence_dir, "filled")

        if dry_run:
            return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))

        if handoff:
            return self._await_handoff_submission(page, entry, evidence_dir, notify)

        return self._submit_and_await_confirmation(page, evidence_dir)

    @staticmethod
    def _confirmed(page) -> bool:
        """The same confirmation signal _submit_and_await_confirmation waits
        for post-click (_CONFIRMATION_SELECTOR), exposed as a poll-able check
        for handoff mode, which never clicks."""
        try:
            return page.locator(_CONFIRMATION_SELECTOR).count() > 0
        except PlaywrightError:
            return False

    def _await_handoff_submission(self, page, entry: dict, evidence_dir: Path, notify) -> PortalResult:
        """Handoff mode: never click #btn-submit -- the human does that
        themselves after solving any CAPTCHA. Notify once, then poll the
        same confirmation signal _submit_and_await_confirmation waits for,
        every 2s up to the handoff timeout. Confirmed -> "submitted" with
        evidence. Timeout -> needs_human, form left exactly as the human
        last saw it (never retried, matching the armed no-confirmation
        degrade)."""
        company = (entry or {}).get("company", "")
        if notify is not None:
            try:
                notify(f"{company} form filled and waiting: solve any captcha and click submit")
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

    def _submit_and_await_confirmation(self, page, evidence_dir: Path) -> PortalResult:
        """Click submit, then wait for a confirmation signal. A pre-click
        error (submit control missing/unclickable) is a genuine "failed" --
        the click itself never happened, so nothing was submitted and the
        orchestrator's ledger-rollback-on-failed is safe. Anything that goes
        wrong AFTER the click is a different story: the click already fired,
        so a plain confirmation timeout OR any other PlaywrightError (page
        closed, navigation interrupted, etc.) is equally ambiguous -- the
        submission may have gone through server-side with nothing to show
        for it client-side. Both must degrade to needs_human, matching
        Ashby/Greenhouse (never "failed" post-click, which would delete the
        ledger row and risk re-submitting a job that may already be in)."""
        try:
            page.locator("#btn-submit").first.click()
        except PlaywrightError as exc:
            return PortalResult(status="failed", reason=f"submit-click:{exc}", evidence_dir=str(evidence_dir))

        try:
            page.wait_for_selector(_CONFIRMATION_SELECTOR, timeout=_CONFIRMATION_TIMEOUT_MS)
        except PlaywrightError:
            return PortalResult(status="needs_human", reason=_NO_CONFIRMATION_REASON,
                                 evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "submitted")
        return PortalResult(status="submitted", reason="", evidence_dir=str(evidence_dir))


register_adapter(LeverAdapter())
