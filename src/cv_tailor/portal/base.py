# src/cv_tailor/portal/base.py
"""Portal adapter base + Playwright infra for autonomous ATS applications.

`run_portal_application` owns the entire browser lifecycle for one job: it
launches headless chromium, navigates to the entry's apply_target, checks for
a CAPTCHA/login wall before anything is typed, dispatches to the registered
adapter for that host, and always writes evidence (screenshot + form_state
dump) before returning -- success, blocked, timeout, or crash. Nothing here
ever submits a form; that is entirely the adapter's decision, gated by the
`dry_run` flag it receives (build-time calls always pass dry_run=True per the
Global Constraints -- the first real submit happens only when Teodor approves
a real job while armed, from the orchestrator built in a later task).

Per-ATS adapters (Ashby/Greenhouse/Lever, later tasks) subclass PortalAdapter
and call `register_adapter(SomeAdapter())` at import time. `adapter_for`
matches the URL's parsed hostname against `hosts` (exact or subdomain), so
registration order only matters if two adapters' hosts could ever both match
the same hostname (they should not).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, NamedTuple

from cv_tailor.urlsafe import host_matches, safe_hostname
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class PortalResult(NamedTuple):
    status: str        # "submitted" | "filled" | "needs_human" | "failed"
    reason: str         # "" on success; e.g. "captcha", "login-required", "unanswerable-required:<label>", "timeout", error text
    evidence_dir: str   # package_dir/portal/, always populated with whatever was captured


class PortalAdapter:
    """Base class for one ATS's fill/submit logic. Subclasses set `hosts`
    (hostnames this adapter claims, matched exactly or as a subdomain
    suffix) and `name`, and implement `apply`.

    `client`/`deployment` are optional: None means the LLM tier of
    cv_tailor.screening.answer_question is unreachable and screening runs
    deterministic-tier-only (a required question with no grounded answer
    honestly aborts to needs_human rather than guessing). The orchestrator
    passes its own Azure client through run_portal_application so the LLM
    tier is reachable in production; every adapter's own tests keep
    exercising the deterministic-only (client=None) path by simply omitting
    the kwarg."""

    hosts: tuple[str, ...] = ()
    name: str = ""

    def apply(self, page, entry: dict, package: dict, profile: dict,
              answers: dict, *, dry_run: bool, client: Any = None,
              deployment: str | None = None, handoff: bool = False,
              notify: Any = None) -> PortalResult:
        raise NotImplementedError


_REGISTRY: list[PortalAdapter] = []


def register_adapter(adapter: PortalAdapter) -> PortalAdapter:
    """Add an adapter instance to the hostname registry. Returns the
    adapter unchanged so it can be used as `SOME_ADAPTER = register_adapter(X())`."""
    _REGISTRY.append(adapter)
    return adapter


def adapter_for(url: str) -> PortalAdapter | None:
    """First registered adapter whose `hosts` matches the URL's browser-faithful
    hostname (exact or subdomain), or None when no adapter claims it (caller
    degrades to needs_human). apply_target URLs come from open job boards, so
    this must never match a lookalike that merely embeds an allowed name
    (jobs.ashbyhq.com.evil.com), a userinfo trick, or a parser-differential
    host (backslash/whitespace) that urlparse and the browser resolve
    differently -- safe_hostname returns "" for those, and "" never matches."""
    host = safe_hostname(url)
    for adapter in _REGISTRY:
        if any(host_matches(host, allowed) for allowed in adapter.hosts):
            return adapter
    return None


# --- blocker detection ------------------------------------------------------

# A captcha blocks only when there is something to SOLVE.
#
# The invisible/score-based reCAPTCHA badge is not a wall: it sits on
# essentially every Ashby and Greenhouse application form and resolves on
# submit with no human interaction. Measured live 2026-09-16 on a real Ashby
# form (Cohere) and a real Greenhouse EU form (saas.group): the only
# captcha-ish element on either page was that badge (inside .grecaptcha-badge,
# 256x60, parked at the page edge, src .../api2/anchor or
# .../enterprise/anchor), neither had an iframe[src*='bframe'] challenge, and
# both had a working Submit button.
#
# Matching the badge made detect_blockers return "captcha" on those forms and
# abort BEFORE filling a single field -- that is what produced both of that
# day's captcha parks, on forms that were sitting there fillable, and it is
# very likely behind the older "greenhouse and lever are captcha-walled"
# belief too.
#
# So: the challenge dialog (bframe) blocks, and a widget that is NOT inside
# the badge container blocks. The badge alone does not. Still selector-only,
# because that is all detect_blockers may rely on.
_CAPTCHA_SELECTORS = (
    # the reCAPTCHA challenge dialog itself -- only rendered when a human
    # actually has to solve something
    "iframe[src*='bframe']",
    # a recaptcha widget that is not the invisible badge (e.g. a v2 checkbox)
    "iframe[src*='recaptcha']:not(.grecaptcha-badge *)",
    ".g-recaptcha:not(.grecaptcha-badge)",
    # hcaptcha and turnstile have no equivalent always-present badge on these
    # boards, so they are unchanged: Lever's hCaptcha gate still blocks.
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare.com']",
    ".h-captcha",
    ".cf-turnstile",
)
_LOGIN_SELECTOR = "input[type='password']"


def detect_blockers(page) -> str | None:
    """"captcha" | "login-required" | None. Checked right after navigation
    and again by adapters mid-flow if a click reveals a new wall. Never
    raises -- a selector engine error on one candidate just skips it."""
    for selector in _CAPTCHA_SELECTORS:
        try:
            if page.locator(selector).count() > 0:
                return "captcha"
        except PlaywrightError:
            continue
    try:
        if page.locator(_LOGIN_SELECTOR).count() > 0:
            return "login-required"
    except PlaywrightError:
        pass
    return None


# --- handoff mode -------------------------------------------------------------
#
# Handoff mode: a human sits at a HEADED browser window and does only the
# CAPTCHA and the final submit click; everything else (filling, upload,
# screening answers) is still driven by the adapter, exactly like an armed
# run. These two helpers give the three adapters a single, shared way to
# (a) wait out a blocker until a human clears it or gives up, and (b) turn
# that wait into the right PortalResult without duplicating the polling loop
# three times.

def handoff_timeout_s() -> float:
    """APPLY_HANDOFF_TIMEOUT seconds, read fresh on every call (never cached
    at import time, so tests -- and prod config -- can vary it per-run).
    Governs both the blocker-wait and the submit-wait. Default 600s."""
    try:
        return float(os.environ.get("APPLY_HANDOFF_TIMEOUT", "600"))
    except (TypeError, ValueError):
        return 600.0


def manual_handoff_timeout_s() -> float:
    """APPLY_MANUAL_HANDOFF_TIMEOUT seconds, read fresh on every call (same
    never-cached contract as handoff_timeout_s). Default 3600s.

    Deliberately a SEPARATE budget from handoff_timeout_s, because the two
    measure different human tasks. handoff_timeout_s's 600s is "how long to
    wait for someone to solve a CAPTCHA or click submit" on a form the adapter
    already filled -- generous for that. The no-adapter manual handoff is the
    ENTIRE application done by hand: find the posting on the site, fill every
    field, upload a CV, answer screening questions. At 600s that wait returns
    False, the finally runs, and browser.close() takes the window away from a
    human mid-form. An hour is the sane floor for that, and it lives here in
    version control rather than in a .env nobody has set."""
    try:
        return float(os.environ.get("APPLY_MANUAL_HANDOFF_TIMEOUT", "3600"))
    except (TypeError, ValueError):
        return 3600.0


def wait_for_blocker_clear(page, timeout_s: float, notify=None) -> bool:
    """When a blocker is detected in handoff mode: notify the human once
    (best-effort -- a notify failure never aborts the wait), then poll
    detect_blockers every 2s until it clears (True) or timeout_s elapses
    (False). Mirrors the polling shape of Ashby's own
    _submit_and_await_confirmation loop."""
    if notify is not None:
        try:
            notify("captcha waiting in the browser on the mini")
        except Exception:  # noqa: BLE001 -- notify is best-effort
            pass

    deadline = time.monotonic() + timeout_s
    while True:
        if detect_blockers(page) is None:
            return True
        if time.monotonic() >= deadline:
            return False
        try:
            page.wait_for_timeout(2000)
        except PlaywrightError:
            return False


def wait_for_human_close(page, timeout_s: float, notify=None) -> bool:
    """Handoff on a host no adapter claims: the browser is open at the posting
    and the human does the whole application. There is no adapter to detect a
    confirmation, so the only honest completion signal is the human closing the
    tab. Poll every 2s until it closes (True) or timeout_s elapses (False).

    Either way the caller returns needs_human -- this function decides how long
    to hold the browser open, never whether an application happened. Mirrors
    wait_for_blocker_clear's polling shape; notify is best-effort and fires
    once, before the wait."""
    if notify is not None:
        try:
            notify("no adapter for this posting -- a browser is open on the mini, "
                   "apply manually and close the tab when done")
        except Exception:  # noqa: BLE001 -- notify is best-effort
            pass

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if page.is_closed():
                return True
        except PlaywrightError:
            return True
        if time.monotonic() >= deadline:
            return False
        try:
            page.wait_for_timeout(2000)
        except PlaywrightError:
            return False


def resolve_blocker(page, blocker: str, evidence_dir, *, stage: str,
                     handoff: bool, notify=None) -> PortalResult | None:
    """Shared blocker-handling policy for an adapter's own mid-flow
    detect_blockers check. `stage` is the evidence filename to use on the
    non-handoff/immediate path -- callers differ here (Ashby captures the
    raw blocker string, Greenhouse/Lever capture the literal "blocked").

    Non-handoff: identical to every adapter's pre-existing inline behavior --
    capture evidence at `stage` and return needs_human(blocker) immediately.
    Handoff: wait_for_blocker_clear up to the handoff timeout. Cleared ->
    None, meaning "continue the flow unchanged" (the caller keeps going).
    Timeout -> capture "aborted" evidence and return
    needs_human("handoff-timeout: captcha not solved") -- never retried,
    matching every other needs_human degrade in this package."""
    if not handoff:
        capture_evidence(page, evidence_dir, stage)
        return PortalResult(status="needs_human", reason=blocker, evidence_dir=str(evidence_dir))

    if wait_for_blocker_clear(page, handoff_timeout_s(), notify):
        return None

    capture_evidence(page, evidence_dir, "aborted")
    return PortalResult(status="needs_human", reason="handoff-timeout: captcha not solved",
                         evidence_dir=str(evidence_dir))


# --- evidence capture --------------------------------------------------------

def _dump_form_state(page) -> dict:
    try:
        return page.eval_on_selector_all(
            "input[name], select[name], textarea[name]",
            "els => Object.fromEntries(els.map(el => [el.name, el.value]))",
        )
    except PlaywrightError:
        return {}


def capture_evidence(page, evidence_dir, stage: str) -> None:
    """Best-effort: write `<stage>.png` (full-page screenshot) and overwrite
    `form_state.json` with the current named-field name->value snapshot.
    Never raises, full stop -- this runs from a `finally` block in
    run_portal_application, so an evidence-write failure (mkdir on a bad
    path, a full disk on write_text, a closed page) must never mask the
    real result underneath it. A failed screenshot still lets the
    form_state write proceed, and vice versa, so a crash mid-run loses only
    whichever half of the evidence really was uncapturable; if the whole
    body blows up (e.g. mkdir itself fails), this returns having captured
    nothing rather than raising."""
    try:
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)

        try:
            page.screenshot(path=str(evidence_dir / f"{stage}.png"), full_page=True)
        except PlaywrightError:
            pass

        form_state = _dump_form_state(page)
        (evidence_dir / "form_state.json").write_text(
            json.dumps(form_state, indent=2, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001 -- best-effort evidence capture must never raise (see docstring)
        pass


# --- field filling ------------------------------------------------------------

def fill_field(page, selector: str, value) -> bool:
    """Fill the first element matching `selector` with `value`. Returns True
    on success, False if the value is empty or the field is missing/not
    fillable -- never raises, so adapters can treat an optional field's
    absence as a non-event and a REQUIRED field's False as the trigger for
    needs_human."""
    if not value:
        return False
    try:
        locator = page.locator(selector).first
        if locator.count() == 0:
            return False
        locator.fill(str(value))
        return True
    except PlaywrightError:
        return False


# --- write verification -------------------------------------------------------
#
# Root cause these two helpers close: adapters used to treat "a grounded answer
# was obtained" as "the value is in the DOM". It is not the same thing -- a
# readonly/reverting field, a stale selector, or an upload that never attached
# all leave the form blank while the code believes it filled it. verify_filled /
# verify_file_attached read the browser back so an adapter can prove a write
# landed before it declares a form filled or (armed) clicks submit. Both are
# best-effort readers: any miss/error returns False so the caller treats a
# failed read-back exactly like a failed write.

def verify_filled(page, selector: str, expected) -> bool:
    """True iff the first element matching `selector` currently holds
    `expected` (case- and whitespace-normalized). Reads the live value back
    via `input_value()` (which also returns a <select>'s selected option
    value), so it proves the DOM actually took the write rather than trusting
    that `.fill()`/`select_option` was called. Returns False on an empty
    `expected`, a missing element, or any Playwright error -- never raises."""
    if expected is None or expected == "":
        return False
    try:
        locator = page.locator(selector).first
        if locator.count() == 0:
            return False
        actual = locator.input_value()
    except PlaywrightError:
        return False
    return str(actual).strip().casefold() == str(expected).strip().casefold()


def verify_file_attached(page, selector: str) -> bool:
    """True iff the file input matching `selector` has at least one file
    attached (`el.files && el.files.length > 0`). This is the only reliable
    read-back for an upload: browsers never expose a file input's value via
    `.value`/`input_value()`, so the resume upload can only be confirmed by
    inspecting `el.files`. Returns False on a missing element or any error --
    never raises."""
    try:
        locator = page.locator(selector).first
        if locator.count() == 0:
            return False
        return bool(locator.evaluate("el => !!(el.files && el.files.length > 0)"))
    except PlaywrightError:
        return False


def id_selector(element_id: str) -> str:
    """A selector matching an element by id, valid for ANY id.

    "#" + id is not safe: a CSS id selector cannot begin with a digit, and
    Ashby's custom-question ids are UUIDs -- roughly a third of which start
    with one. Building "#5d69bc56-0ca7-49e3-b539-ce4c829fd8fa" raises
    SyntaxError ("not a valid selector") rather than matching nothing, so the
    failure did not look like a selector problem at all: it surfaced on a real
    posting as unwritable-required, with the answer composed, the write
    attempted, and the verification blowing up on the way back.

    The attribute form matches regardless of the first character. Backslashes
    and double quotes are escaped so an id from an untrusted page cannot break
    out of the quoted value.
    """
    escaped = str(element_id).replace("\\", "\\\\").replace('"', '\\"')
    return f'[id="{escaped}"]'


def screening_context(package: dict) -> dict:
    """Material the composed free-text screening tier may answer FROM.

    `package` is the assembled meta, so the cover letter written for THIS job
    and its one-line pitch are already in hand. That letter is the only
    grounded prose in his voice that is ALREADY being sent with this
    application, which is what makes composing a long-form answer from it a
    restatement rather than a fresh claim about him.

    Returns {} when there is no readable letter. With nothing to ground an
    answer in, the composed tier must not run at all, and a required
    open-ended question parks exactly as it does today -- the fail-closed
    direction.

    Built from builtins only (no Path, no yaml): this runs on every portal
    application, and a helper that gathers evidence must never be the thing
    that raises.
    """
    path = (package or {}).get("cover_letter_path")
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            letter = handle.read().strip()
    except OSError:
        return {}
    if not letter:
        return {}
    context = {"cover_letter": letter}
    pitch = str((package or {}).get("one_line_pitch") or "").strip()
    if pitch:
        context["pitch"] = pitch
    return context


# --- orchestration ------------------------------------------------------------

def run_portal_application(entry: dict, package: dict, profile: dict, answers: dict, *,
                            dry_run: bool, timeout_s: int = 120, headless: bool = True,
                            client: Any = None, deployment: str | None = None,
                            handoff: bool = False, notify: Any = None) -> PortalResult:
    """Own the full Playwright lifecycle for one job's portal application.

    Flow: resolve evidence_dir -> resolve URL (absent or not http(s) ->
    needs_human("missing-apply-target")) -> look up the adapter for its
    host (no match, unattended -> needs_human("no-adapter") before any
    browser is launched; no match, handoff -> keep going, see below) ->
    launch chromium -> navigate (timeout -> needs_human) -> [handoff with no
    adapter returns here, before the blocker check] -> blocker check
    (captcha/login-required -> needs_human) -> dispatch to the adapter
    (timeout -> needs_human) -> capture evidence at whatever stage was last
    reached, always, even on an adapter exception -> any uncaught exception
    anywhere degrades to "failed" rather than propagating, so a bad job
    never kills the caller's batch run.

    `client`/`deployment` are forwarded to the adapter unchanged (None by
    default, meaning screening.answer_question runs deterministic-tier
    only). The orchestrator passes its own Azure client here so screening
    questions the deterministic tier can't resolve get an LLM-grounded
    shot before the caller falls back to needs_human.

    `handoff=True` is "a human is watching this browser and will do the
    CAPTCHA + submit click themselves": it forces a HEADED browser
    (`headless` is overridden to False, whatever the caller passed) and
    forwards `handoff`/`notify` to the adapter unchanged, same as
    `client`/`deployment` above -- the adapter owns all handoff-specific
    waiting (see cv_tailor.portal.base.wait_for_blocker_clear /
    resolve_blocker). `notify` is a best-effort `str -> None` callable (e.g.
    Telegram) the adapter uses to tell the human what it's waiting on.

    Handoff also relaxes the adapter gate: a human can apply by hand on ANY
    site, so a host no adapter claims still gets a headed browser at the
    posting, held open by wait_for_human_close until the tab closes or the
    handoff timeout expires. That path returns BEFORE the blocker check (a
    captcha/login is the human's to solve when nothing is being automated)
    and captures its own "handoff" evidence up front, since the tab is
    usually gone by the time the finally runs. It always returns
    needs_human("handoff-manual: no adapter") -- deliberately NOT the
    unattended "no-adapter" string, because a human at a real browser may
    genuinely have submitted, so callers must not treat it as proof that
    nothing was sent.

    `timeout_s` is a wall-clock budget for the whole browser interaction,
    not a per-action cap: navigation gets the full budget, but once it
    returns the page's default action timeout shrinks to whatever remains
    (floored at 5s) so a chain of slow adapter actions can't each burn the
    full budget on their own. In handoff mode that remaining budget is
    extended by APPLY_HANDOFF_TIMEOUT (see handoff_timeout_s) before the
    floor is applied, so the overall run timeout can never cut off a human
    still solving a CAPTCHA or about to click submit. A Playwright
    TimeoutError raised anywhere in navigation or adapter dispatch degrades
    to needs_human("timeout") rather than the generic "failed" -- it means
    the page never responded in time, not that the adapter's logic is
    broken. If the adapter finishes and returns normally despite running
    past timeout_s, its result stands as-is: the cap only governs blocking
    waits, not elapsed wall-clock time after the fact.
    """
    if handoff:
        headless = False
    evidence_dir = Path(package["package_dir"]) / "portal"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # Absent, or not an http(s) URL. The scheme check is fail-closed and
    # matters most in handoff mode, which is the only path that navigates to a
    # host no adapter claims -- apply_target comes from an untrusted feed, and
    # a file:// / javascript: target must never reach page.goto. Unattended
    # behaviour is preserved: such a URL already degraded to needs_human
    # (safe_hostname -> "" -> no adapter), and "missing-apply-target" and
    # "no-adapter" are both needs_human and both in apply_approved's
    # _NO_SUBMIT_REASONS, so the ledger outcome is identical.
    url = (entry.get("apply_target") or entry.get("url") or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return PortalResult(status="needs_human", reason="missing-apply-target",
                             evidence_dir=str(evidence_dir))

    adapter = adapter_for(url)
    # Unattended: no adapter means nothing can be filled, so don't pay for a
    # browser. Handoff: a human is watching and can apply by hand on ANY site,
    # so the browser is the entire point of the button -- open it at the
    # posting and let them work (see the no-adapter branch after navigation).
    if adapter is None and not handoff:
        return PortalResult(status="needs_human", reason="no-adapter",
                             evidence_dir=str(evidence_dir))

    page = None
    stage = "start"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            try:
                page = browser.new_page()
                page.set_default_timeout(timeout_s * 1000)

                start = time.monotonic()
                try:
                    page.goto(url, wait_until="load", timeout=timeout_s * 1000)
                except PlaywrightTimeoutError:
                    stage = "timeout"
                    return PortalResult(status="needs_human", reason="timeout",
                                         evidence_dir=str(evidence_dir))

                # Shrink the per-action budget to whatever's left of timeout_s
                # (floored at 5s) so a chain of adapter actions can't each
                # burn the full wall-clock budget on their own. In handoff
                # mode, extend that remaining budget by the handoff timeout
                # first -- the human's own CAPTCHA/submit wait must never be
                # cut off by this shrink.
                remaining = timeout_s - (time.monotonic() - start)
                if handoff:
                    remaining += handoff_timeout_s()
                page.set_default_timeout(max(remaining, 5) * 1000)

                if adapter is None:
                    # Handoff with no adapter: hand the open page to the human.
                    # Always needs_human -- we cannot observe whether they
                    # submitted, and claiming "sent" on a guess would poison
                    # both the ledger and the CRM. The distinct reason string
                    # keeps this OUT of apply_approved's _NO_SUBMIT_REASONS:
                    # unlike an unattended no-adapter abort, a real submission
                    # may well have happened here.
                    #
                    # This runs BEFORE detect_blockers on purpose. Blocker
                    # detection exists to stop an AUTOMATED fill from crashing
                    # into a wall; here there is no automation, so a captcha or
                    # login is the human's to solve, not ours to detect.
                    # Detecting one would be actively harmful: _LOGIN_SELECTOR
                    # matches any password input on the page (a header login
                    # box, a footer form), so resolve_blocker would burn the
                    # whole handoff timeout waiting for a human to "clear"
                    # something that was never in the way and then return
                    # "handoff-timeout: captcha not solved" -- which IS in
                    # _NO_SUBMIT_REASONS, deleting the ledger row for an
                    # application the human may well have submitted.
                    stage = "handoff"
                    # Capture before the wait, not only in the finally: the
                    # happy path ends with the human closing the tab, so the
                    # finally's screenshot hits a closed page and handoff.png
                    # would be missing exactly when the run went well. The
                    # posting as opened is the better artifact anyway -- it is
                    # proof the browser landed on the right page. The finally
                    # then overwrites form_state.json with {}, which is fine:
                    # nothing was filled, so the form dump has no value here.
                    capture_evidence(page, evidence_dir, "handoff")
                    # manual_handoff_timeout_s, NOT handoff_timeout_s: this
                    # wait spans the whole application done by hand, not a
                    # CAPTCHA on an already-filled form. Reusing the 600s
                    # captcha budget here closes the browser on a human who is
                    # still typing.
                    wait_for_human_close(page, manual_handoff_timeout_s(), notify)
                    return PortalResult(status="needs_human",
                                         reason="handoff-manual: no adapter",
                                         evidence_dir=str(evidence_dir))

                blocker = detect_blockers(page)
                if blocker:
                    stage = blocker
                    # Same semantics as the adapters' in-flow checks: handoff
                    # waits for the human to clear the blocker (None -> keep
                    # going to dispatch); non-handoff aborts immediately.
                    blocked = resolve_blocker(page, blocker, evidence_dir,
                                              stage=blocker, handoff=handoff,
                                              notify=notify)
                    if blocked is not None:
                        return blocked

                stage = "dispatch"
                try:
                    result = adapter.apply(page, entry, package, profile, answers, dry_run=dry_run,
                                            client=client, deployment=deployment,
                                            handoff=handoff, notify=notify)
                except PlaywrightTimeoutError:
                    stage = "timeout"
                    return PortalResult(status="needs_human", reason="timeout",
                                         evidence_dir=str(evidence_dir))
                stage = result.status
                return result
            finally:
                if page is not None:
                    capture_evidence(page, evidence_dir, stage)
                browser.close()
    except Exception as exc:  # noqa: BLE001 -- any browser/adapter failure degrades to "failed", never crashes the caller
        return PortalResult(status="failed", reason=f"{type(exc).__name__}: {exc}",
                             evidence_dir=str(evidence_dir))
