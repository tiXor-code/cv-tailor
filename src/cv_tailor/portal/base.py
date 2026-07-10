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
matches by substring against `hosts`, so registration order only matters if
two adapters' host substrings could ever both match the same URL (they
should not).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, NamedTuple

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class PortalResult(NamedTuple):
    status: str        # "submitted" | "filled" | "needs_human" | "failed"
    reason: str         # "" on success; e.g. "captcha", "login-required", "unanswerable-required:<label>", "timeout", error text
    evidence_dir: str   # package_dir/portal/, always populated with whatever was captured


class PortalAdapter:
    """Base class for one ATS's fill/submit logic. Subclasses set `hosts`
    (url substrings this adapter claims) and `name`, and implement `apply`.

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
              deployment: str | None = None) -> PortalResult:
        raise NotImplementedError


_REGISTRY: list[PortalAdapter] = []


def register_adapter(adapter: PortalAdapter) -> PortalAdapter:
    """Add an adapter instance to the host-substring registry. Returns the
    adapter unchanged so it can be used as `SOME_ADAPTER = register_adapter(X())`."""
    _REGISTRY.append(adapter)
    return adapter


def adapter_for(url: str) -> PortalAdapter | None:
    """First registered adapter whose `hosts` substring appears in `url`,
    or None when no adapter claims this host (caller degrades to needs_human)."""
    for adapter in _REGISTRY:
        if any(host in url for host in adapter.hosts):
            return adapter
    return None


# --- blocker detection ------------------------------------------------------

_CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    "iframe[src*='challenges.cloudflare.com']",
    ".g-recaptcha",
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


# --- orchestration ------------------------------------------------------------

def run_portal_application(entry: dict, package: dict, profile: dict, answers: dict, *,
                            dry_run: bool, timeout_s: int = 120, headless: bool = True,
                            client: Any = None, deployment: str | None = None) -> PortalResult:
    """Own the full Playwright lifecycle for one job's portal application.

    Flow: resolve evidence_dir -> resolve URL -> look up the adapter for its
    host (no match -> needs_human before any browser is launched) -> launch
    headless chromium -> navigate (timeout -> needs_human) -> blocker check
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

    `timeout_s` is a wall-clock budget for the whole browser interaction,
    not a per-action cap: navigation gets the full budget, but once it
    returns the page's default action timeout shrinks to whatever remains
    (floored at 5s) so a chain of slow adapter actions can't each burn the
    full budget on their own. A Playwright TimeoutError raised anywhere in
    navigation or adapter dispatch degrades to needs_human("timeout")
    rather than the generic "failed" -- it means the page never responded
    in time, not that the adapter's logic is broken. If the adapter
    finishes and returns normally despite running past timeout_s, its
    result stands as-is: the cap only governs blocking waits, not elapsed
    wall-clock time after the fact.
    """
    evidence_dir = Path(package["package_dir"]) / "portal"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    url = (entry.get("apply_target") or entry.get("url") or "").strip()
    if not url:
        return PortalResult(status="needs_human", reason="missing-apply-target",
                             evidence_dir=str(evidence_dir))

    adapter = adapter_for(url)
    if adapter is None:
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
                # burn the full wall-clock budget on their own.
                remaining = timeout_s - (time.monotonic() - start)
                page.set_default_timeout(max(remaining, 5) * 1000)

                blocker = detect_blockers(page)
                if blocker:
                    stage = blocker
                    return PortalResult(status="needs_human", reason=blocker,
                                         evidence_dir=str(evidence_dir))

                stage = "dispatch"
                try:
                    result = adapter.apply(page, entry, package, profile, answers, dry_run=dry_run,
                                            client=client, deployment=deployment)
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
