# src/cv_tailor/portal/greenhouse.py
"""Greenhouse Job Board adapter.

Both live hosts (the legacy `boards.greenhouse.io` and the current
`job-boards.greenhouse.io`, which the legacy host now redirects to) serve
the same React application form, and that form lives directly on the
posting page -- there is no separate "click Apply, navigate elsewhere"
step like Ashby's tab or Lever's `/apply` path, so `apply()` never
navigates on its own.

Field ids/labels are faithful to a real posting (job-boards.greenhouse.io
gitlab/remotecom/canonical, fetched while building the fixture):
first_name/last_name/email/phone contact inputs, a resume file input, an
optional cover_letter file input, and custom screening questions whose
ids are prefixed `question_<numeric-id>` and whose label lives in the
control's own `aria-label` attribute with `aria-required` marking whether
an answer is mandatory. Greenhouse splits the name into two fields, so
`profile.contact.name` is split on the first space.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

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
from cv_tailor.screening import Question, answer_question

# Enumerates every `question_<id>` control on the page in one round trip:
# native <select> -> kind "select" (options = visible option text, minus
# the empty placeholder); <textarea> -> "textarea"; a number-typed <input>
# -> "number"; everything else -> "text". Label comes from aria-label
# (exactly how Greenhouse's real custom-question markup carries it) and
# required comes from aria-required, both read as attributes so this works
# whether the element is a real Greenhouse control or this fixture's plain
# HTML stand-ins.
_QUESTION_JS = """
els => els.map(el => {
  const tag = el.tagName.toLowerCase();
  let kind = "text";
  let options = [];
  if (tag === "select") {
    kind = "select";
    options = Array.from(el.options)
      .filter(o => o.value !== "")
      .map(o => o.textContent.trim());
  } else if (tag === "textarea") {
    kind = "textarea";
  } else if (el.type === "number") {
    kind = "number";
  }
  return {
    id: el.id,
    label: el.getAttribute("aria-label") || "",
    kind,
    required: el.getAttribute("aria-required") === "true",
    options,
  };
})
"""


def _split_name(full_name: str) -> tuple[str, str]:
    """Greenhouse wants first/last separately; profile.contact.name is one
    string. Split on the first space; a single-word name gets an empty
    last name rather than guessing."""
    parts = (full_name or "").strip().split(" ", 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _upload_file(page, selector: str, path: Any) -> bool:
    """set_input_files counterpart to base.fill_field: same never-raise,
    return-True/False-on-success contract, for the one action fill_field
    does not cover (file inputs use set_input_files, not .fill())."""
    if not path:
        return False
    try:
        locator = page.locator(selector)
        if locator.count() == 0:
            return False
        locator.set_input_files(str(path))
        return True
    except PlaywrightError:
        return False


def _enumerate_questions(page) -> list[tuple[str, Question]]:
    try:
        raw = page.eval_on_selector_all('[id^="question_"]', _QUESTION_JS)
    except PlaywrightError:
        return []
    out = []
    for item in raw:
        field_id = item.get("id")
        label = item.get("label")
        if not field_id or not label:
            continue
        out.append((
            field_id,
            Question(
                label=label,
                kind=item.get("kind", "text"),
                required=bool(item.get("required")),
                options=tuple(item.get("options") or ()),
            ),
        ))
    return out


class GreenhouseAdapter(PortalAdapter):
    hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io")
    name = "greenhouse"

    # Real submit-confirmation wait; tests override this on the instance to
    # keep the no-confirmation-timeout case fast rather than actually
    # waiting 30s.
    CONFIRM_TIMEOUT_MS = 30_000

    def apply(self, page, entry: dict, package: dict, profile: dict,
              answers: dict, *, dry_run: bool) -> PortalResult:
        evidence_dir = Path(package["package_dir"]) / "portal"

        # Greenhouse's real Job Board is a React app: the "load" event
        # (which run_portal_application already waited for) fires before
        # the form finishes hydrating and its job data fetch resolves, and
        # filling a controlled input before its onChange handler is
        # attached gets silently reverted by the next re-render. A short
        # networkidle wait settles this; a static fixture page has nothing
        # left in flight so this returns immediately there. Never let a
        # page with some permanent background connection (analytics, a
        # polling widget) block the whole run -- proceed anyway on timeout.
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except PlaywrightError:
            pass

        # The form is already on the posting page (no navigation to do),
        # but a captcha can still appear post-load (e.g. a challenge
        # injected once the page settles), so check again defensively --
        # this is the adapter-level half of "detect_blockers early and
        # after navigation"; run_portal_application already checked once
        # right after page.goto before dispatching here.
        blocker = detect_blockers(page)
        if blocker:
            capture_evidence(page, evidence_dir, "blocked")
            return PortalResult(status="needs_human", reason=blocker,
                                 evidence_dir=str(evidence_dir))

        contact = (profile or {}).get("contact", {}) or {}
        first_name, last_name = _split_name(contact.get("name", ""))
        fill_field(page, "#first_name", first_name)
        fill_field(page, "#last_name", last_name)
        fill_field(page, "#email", contact.get("email", ""))
        fill_field(page, "#phone", contact.get("phone", ""))

        _upload_file(page, "#resume", package.get("cv_path"))
        if page.locator("#cover_letter").count() > 0:
            _upload_file(page, "#cover_letter", package.get("cover_letter_path"))

        for field_id, question in _enumerate_questions(page):
            # No LLM client is wired into the adapter (apply()'s signature
            # is fixed by the base class), so answer_question always runs
            # deterministic-tier-only -- per its own contract that means
            # ANY ungrounded question returns None, not just required ones
            # (client=None: "anything that would need the LLM returns
            # None"). Only abort when the question was actually required;
            # an ungrounded optional question is a normal skip.
            answer = answer_question(question, profile, answers)
            if answer is None:
                if not question.required:
                    continue
                capture_evidence(page, evidence_dir, "aborted")
                return PortalResult(
                    status="needs_human",
                    reason=f"unanswerable-required:{question.label}",
                    evidence_dir=str(evidence_dir),
                )
            if not answer.value:
                continue  # optional + ungrounded -> leave blank, not a failure
            selector = f"#{field_id}"
            if question.kind == "select":
                try:
                    page.locator(selector).select_option(label=answer.value)
                except PlaywrightError:
                    pass
            else:
                fill_field(page, selector, answer.value)

        capture_evidence(page, evidence_dir, "filled")

        if dry_run:
            return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))

        try:
            page.locator("#submit_app").click()
            page.wait_for_selector("#confirmation-message", timeout=self.CONFIRM_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            return PortalResult(status="needs_human", reason="no-confirmation",
                                 evidence_dir=str(evidence_dir))
        except PlaywrightError:
            # Submit control missing/unclickable etc. -- same fallback: the
            # submission may or may not have gone through, never retry.
            return PortalResult(status="needs_human", reason="no-confirmation",
                                 evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "submitted")
        return PortalResult(status="submitted", reason="", evidence_dir=str(evidence_dir))


register_adapter(GreenhouseAdapter())
