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


def _is_required(block, field=None) -> bool:
    if block.locator(".required").count() > 0:
        return True
    if field is not None and field.count() > 0:
        try:
            return field.first.get_attribute("required") is not None
        except PlaywrightError:
            return False
    return False


def _label_for(block) -> str:
    label = block.locator(".application-label").first
    if label.count() == 0:
        return ""
    try:
        return _clean_label(label.inner_text())
    except PlaywrightError:
        return ""


def _options_for(locators) -> tuple[str, ...]:
    values = []
    for j in range(locators.count()):
        try:
            values.append((locators.nth(j).get_attribute("value") or "").strip())
        except PlaywrightError:
            continue
    return tuple(v for v in values if v)


def discover_questions(page) -> list[tuple[Question, str]]:
    """Walk every `.application-question` block not already covered by the
    direct-fill step and turn it into a (Question, field_name) pair. Lever
    marks standard/custom-card questions with `<li class="application-
    question">` but EEO questions with `<div class="application-question">`
    -- the class selector (not tag-scoped) catches both. Blocks with no
    named form control (e.g. Lever's "Apply with LinkedIn" autofill widget)
    yield nothing -- there is nothing to answer or fill."""
    out: list[tuple[Question, str]] = []
    blocks = page.locator(".application-question")

    for i in range(blocks.count()):
        block = blocks.nth(i)

        select = block.locator("select")
        if select.count() > 0:
            name = select.first.get_attribute("name") or ""
            if name and name not in _DIRECT_FILL_NAMES:
                options = _options_for(block.locator("select option"))
                out.append((Question(_label_for(block), "select", _is_required(block), options), name))
            continue

        radios = block.locator("input[type='radio']")
        if radios.count() > 0:
            name = radios.first.get_attribute("name") or ""
            if name and name not in _DIRECT_FILL_NAMES:
                options = _options_for(radios)
                out.append((Question(_label_for(block), "radio", _is_required(block), options), name))
            continue

        checkboxes = block.locator("input[type='checkbox']")
        if checkboxes.count() > 0:
            name = checkboxes.first.get_attribute("name") or ""
            if name and name not in _DIRECT_FILL_NAMES:
                options = _options_for(checkboxes)
                out.append((Question(_label_for(block), "checkbox", _is_required(block), options), name))
            continue

        field = block.locator("input[type='text'], input[type='email'], input[type='tel'], textarea")
        if field.count() > 0:
            name = field.first.get_attribute("name") or ""
            if name and name not in _DIRECT_FILL_NAMES:
                try:
                    tag = field.first.evaluate("el => el.tagName.toLowerCase()")
                except PlaywrightError:
                    tag = "input"
                kind = "textarea" if tag == "textarea" else "text"
                out.append((Question(_label_for(block), kind, _is_required(block, field), ()), name))
            continue

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

    def _answer_all(self, page, evidence_dir, profile, answers) -> PortalResult | None:
        """Fill every remaining discovered question. Returns a needs_human
        PortalResult if a REQUIRED question can't be grounded, else None."""
        for question, field_name in discover_questions(page):
            answer = answer_question(question, profile, answers, client=self.client, deployment=self.deployment)
            if answer is None:
                capture_evidence(page, evidence_dir, "aborted")
                return PortalResult(status="needs_human",
                                     reason=f"unanswerable-required:{question.label}",
                                     evidence_dir=str(evidence_dir))
            if not answer.value:
                continue
            self._apply_answer(page, question, field_name, answer)
        return None

    @staticmethod
    def _apply_answer(page, question: Question, field_name: str, answer: Answer) -> None:
        if question.kind == "select":
            try:
                page.locator(f"select[name='{field_name}']").first.select_option(label=answer.value)
            except PlaywrightError:
                pass
        elif question.kind in ("radio", "checkbox"):
            try:
                page.locator(
                    f"input[type='{question.kind}'][name='{field_name}'][value='{answer.value}']"
                ).first.check()
            except PlaywrightError:
                pass
        else:
            fill_field(page, f"[name='{field_name}']", answer.value)

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

        cv_path = package.get("cv_path")
        if cv_path:
            try:
                page.locator("input[name='resume']").first.set_input_files(cv_path)
            except PlaywrightError:
                pass

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
