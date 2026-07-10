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
        # client=None (the default) means screening.answer_question runs
        # deterministic-tier only -- a required question with no grounded
        # answer honestly aborts to needs_human rather than guessing. A
        # future orchestrator may set .client/.deployment before calling
        # apply() to enable the LLM tier for questions the deterministic
        # tier can't resolve.
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

    def _answer_all(self, page, evidence_dir, profile, answers) -> PortalResult | None:
        """Fill every remaining discovered question and verify each write.
        Returns a needs_human PortalResult if a REQUIRED question can't be
        grounded (unanswerable-required) or was grounded but the value didn't
        land in the DOM (unwritable-required), else None."""
        for question, field_name in discover_questions(page):
            answer = answer_question(question, profile, answers, client=self.client, deployment=self.deployment)
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
              dry_run: bool) -> PortalResult:
        evidence_dir = Path(package["package_dir"]) / "portal"

        blocker = detect_blockers(page)
        if blocker:
            capture_evidence(page, evidence_dir, "blocked")
            return PortalResult(status="needs_human", reason=blocker, evidence_dir=str(evidence_dir))

        if page.locator("#application-form").count() == 0:
            apply_url = page.url.rstrip("/") + "/apply"
            try:
                page.goto(apply_url, wait_until="load")
            except PlaywrightTimeoutError:
                capture_evidence(page, evidence_dir, "timeout")
                return PortalResult(status="needs_human", reason="timeout", evidence_dir=str(evidence_dir))

            blocker = detect_blockers(page)
            if blocker:
                capture_evidence(page, evidence_dir, "blocked")
                return PortalResult(status="needs_human", reason=blocker, evidence_dir=str(evidence_dir))

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

        aborted = self._answer_all(page, evidence_dir, profile, answers)
        if aborted is not None:
            return aborted

        capture_evidence(page, evidence_dir, "filled")

        if dry_run:
            return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))

        try:
            page.locator("#btn-submit").first.click()
        except PlaywrightError as exc:
            return PortalResult(status="failed", reason=f"submit-click:{exc}", evidence_dir=str(evidence_dir))

        try:
            page.wait_for_selector(_CONFIRMATION_SELECTOR, timeout=_CONFIRMATION_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            # The submission may have gone through server-side even though
            # no confirmation rendered client-side -- never auto-retry.
            return PortalResult(status="needs_human", reason="no-confirmation", evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "submitted")
        return PortalResult(status="submitted", reason="", evidence_dir=str(evidence_dir))


register_adapter(LeverAdapter())
