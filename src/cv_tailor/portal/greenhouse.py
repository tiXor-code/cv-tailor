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
    verify_file_attached,
    verify_filled,
)
from cv_tailor.screening import Question, answer_question

# Enumerates every `question_<id>` control on the page in one round trip.
#
# Label derivation follows a fallback chain (a control that carries no
# aria-label is common on real boards): aria-label -> associated
# label[for] text -> enclosing <fieldset> <legend> -> placeholder. A control
# with none of these has label "" and the adapter decides per required-ness
# (a required unlabelled control is a needs_human, an optional one is skipped).
#
# Kinds: native <select> -> "select" (options = visible option text minus the
# empty placeholder); <textarea> -> "textarea"; number-typed <input> ->
# "number"; radio/checkbox inputs are grouped by `name` into a single
# "radio"/"checkbox" Question whose options are the group's option VALUES (what
# gets checked); everything else -> "text". `required` is aria-required OR the
# native `required` attribute (for a group, any member being required).
_QUESTION_JS = """
() => {
  const controls = Array.from(document.querySelectorAll(
    'input[id^="question_"], select[id^="question_"], textarea[id^="question_"], ' +
    'input[name^="question_"], select[name^="question_"], textarea[name^="question_"]'
  ));
  function labelFor(el) {
    let l = el.getAttribute && el.getAttribute("aria-label");
    if (l && l.trim()) return l.trim();
    const id = el.id;
    if (id) {
      const lab = document.querySelector('label[for="' + (window.CSS ? CSS.escape(id) : id) + '"]');
      if (lab && lab.textContent.trim()) return lab.textContent.trim();
    }
    const fs = el.closest ? el.closest("fieldset") : null;
    if (fs) {
      const leg = fs.querySelector("legend");
      if (leg && leg.textContent.trim()) return leg.textContent.trim();
    }
    const ph = el.getAttribute && el.getAttribute("placeholder");
    if (ph && ph.trim()) return ph.trim();
    return "";
  }
  function isRequired(el) {
    return el.getAttribute("aria-required") === "true" || el.hasAttribute("required");
  }
  const groups = new Map();   // name -> {name, kind, members}
  const singles = [];
  const seen = new Set();
  for (const el of controls) {
    if (seen.has(el)) continue;
    seen.add(el);
    const type = (el.type || "").toLowerCase();
    if (type === "radio" || type === "checkbox") {
      const name = el.name || el.id;
      if (!groups.has(name)) groups.set(name, {name, kind: type, members: []});
      groups.get(name).members.push(el);
    } else {
      singles.push(el);
    }
  }
  const out = [];
  for (const el of singles) {
    const tag = el.tagName.toLowerCase();
    let kind = "text";
    let options = [];
    if (tag === "select") {
      kind = "select";
      options = Array.from(el.options).filter(o => o.value !== "").map(o => o.textContent.trim());
    } else if (tag === "textarea") {
      kind = "textarea";
    } else if ((el.type || "").toLowerCase() === "number") {
      kind = "number";
    }
    out.push({ id: el.id || "", name: el.name || el.id || "", label: labelFor(el),
               kind, required: isRequired(el), options });
  }
  for (const g of groups.values()) {
    const first = g.members[0];
    let label = "";
    const container = document.getElementById(g.name);
    if (container) label = labelFor(container);
    if (!label) {
      const fs = first.closest ? first.closest("fieldset") : null;
      if (fs) { const leg = fs.querySelector("legend"); if (leg) label = leg.textContent.trim(); }
    }
    if (!label) label = labelFor(first);
    const options = g.members.map(m => m.value).filter(v => v !== "");
    out.push({ id: first.id || "", name: g.name, label, kind: g.kind,
               required: g.members.some(isRequired), options });
  }
  return out;
}
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


def _enumerate_questions(page) -> list[tuple[dict, Question]]:
    """Return (target, Question) for every question_* control. `target` carries
    {id, name, kind} so the adapter can address a text/select control by `#id`
    and a radio/checkbox group by `name`. Entries with an empty label are kept
    (label "") so the adapter can enforce the unlabelled-required policy rather
    than silently dropping a required control it could not label."""
    try:
        raw = page.evaluate(_QUESTION_JS)
    except PlaywrightError:
        return []
    out = []
    for item in raw:
        kind = item.get("kind", "text")
        field_id = item.get("id") or ""
        name = item.get("name") or field_id
        # A text/select/textarea we can't address (no id) is unusable; a
        # radio/checkbox group is addressed by name, so it only needs a name.
        if kind in ("radio", "checkbox"):
            if not name:
                continue
        elif not field_id:
            continue
        out.append((
            {"id": field_id, "name": name, "kind": kind},
            Question(
                label=item.get("label") or "",
                kind=kind,
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
              answers: dict, *, dry_run: bool, client: Any = None,
              deployment: str | None = None) -> PortalResult:
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

        # Verify the two universally-required contact fields landed (name -> the
        # first_name control, email). A grounded value that never reached the
        # DOM must abort, not submit a form missing the applicant's identity.
        if not self._verify_contact(page, first_name, contact.get("email", "")):
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason="contact-fill-failed",
                                 evidence_dir=str(evidence_dir))

        # The resume upload is write-VERIFIED (el.files length). A missing
        # cv_path, a selector that matches nothing, or a file that never
        # attached must abort to needs_human BEFORE any armed submit rather
        # than applying with no resume attached.
        _upload_file(page, "#resume", package.get("cv_path"))
        if not verify_file_attached(page, "#resume"):
            capture_evidence(page, evidence_dir, "aborted")
            return PortalResult(status="needs_human", reason="resume-upload-failed",
                                 evidence_dir=str(evidence_dir))

        if page.locator("#cover_letter").count() > 0:
            _upload_file(page, "#cover_letter", package.get("cover_letter_path"))

        for target, question in _enumerate_questions(page):
            # Label fallback exhausted with nothing derivable: a required
            # control we cannot even label is unsafe to skip silently (it may
            # gate submission) -> needs_human; an optional one is skipped.
            if not question.label:
                if question.required:
                    capture_evidence(page, evidence_dir, "aborted")
                    return PortalResult(status="needs_human", reason="unlabelled-required-field",
                                         evidence_dir=str(evidence_dir))
                continue

            # client is forwarded from apply() (None by default, meaning
            # deterministic-tier-only -- per answer_question's own contract
            # that means ANY ungrounded question returns None, not just
            # required ones). Only abort when the question was actually
            # required; an ungrounded optional question is a normal skip.
            answer = answer_question(question, profile, answers, client=client, deployment=deployment)
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

            written = self._fill_question(page, target, question, answer.value)
            # A REQUIRED answer that did not verify (readonly/reverting field,
            # a stale selector, an unmatched option) must abort -- "grounded"
            # is not "written". Optional writes stay best-effort.
            if not written and question.required:
                capture_evidence(page, evidence_dir, "aborted")
                return PortalResult(status="needs_human",
                                     reason=f"unwritable-required:{question.label}",
                                     evidence_dir=str(evidence_dir))

        capture_evidence(page, evidence_dir, "filled")

        if dry_run:
            return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))
        return self._submit(page, evidence_dir)

    # --- write helpers ---------------------------------------------------------

    @staticmethod
    def _verify_contact(page, first_name: str, email: str) -> bool:
        """Read back name + email after filling. Only non-empty grounded values
        are checked (nothing to verify when a field was left blank on purpose).
        Returns False if either given value did not land in the DOM."""
        if first_name and not verify_filled(page, "#first_name", first_name):
            return False
        if email and not verify_filled(page, "#email", email):
            return False
        return True

    def _fill_question(self, page, target: dict, question: Question, value: str) -> bool:
        """Fill one enumerated question and verify the write landed. Returns
        False on any failure so the caller can enforce the required policy."""
        kind = question.kind
        if kind in ("radio", "checkbox"):
            return self._check_option(page, kind, target["name"], value)
        selector = f"#{target['id']}"
        if kind == "select":
            try:
                page.locator(selector).select_option(label=value)
            except PlaywrightError:
                return False
            # A non-empty selected value confirms an option was actually
            # taken (select_option raises when the label matches nothing).
            try:
                return bool(page.locator(selector).first.input_value())
            except PlaywrightError:
                return False
        if not fill_field(page, selector, value):
            return False
        return verify_filled(page, selector, value)

    @staticmethod
    def _check_option(page, kind: str, name: str, value: str) -> bool:
        """Check the radio/checkbox in group `name` whose VALUE equals `value`,
        then confirm it is checked. The value is matched in Python (never
        interpolated into a selector) so option values containing quotes or
        other CSS metacharacters cannot break the locator."""
        try:
            group = page.locator(f"input[type='{kind}'][name='{name}']")
            for i in range(group.count()):
                el = group.nth(i)
                if (el.get_attribute("value") or "") == value:
                    el.check()
                    return bool(el.is_checked())
        except PlaywrightError:
            return False
        return False

    def _submit(self, page, evidence_dir: Path) -> PortalResult:

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
