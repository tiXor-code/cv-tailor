# tests/test_portal_base.py
"""Portal adapter base: registry, blocker detection, evidence capture, field
filling, and the run_portal_application lifecycle.

Most tests use FakePage/FakeLocator duck-typed stand-ins so the pure
selector/dump/registry logic is exercised without a browser. A handful of
unmarked smoke tests launch a real headless chromium against fixture HTML
served over http.server (tests/fixtures/portal/serve.py) -- these prove the
actual Playwright wiring works, per the task brief's explicit ask.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "portal"))

import cv_tailor.portal.base as portal_base
from cv_tailor.portal import PortalAdapter, PortalResult, adapter_for, run_portal_application
from cv_tailor.portal.base import (
    capture_evidence,
    detect_blockers,
    fill_field,
    register_adapter,
    resolve_blocker,
    verify_file_attached,
    verify_filled,
    wait_for_blocker_clear,
)
from serve import serve_fixtures


# --- fakes --------------------------------------------------------------------

class FakeLocator:
    def __init__(self, count=0, raise_error=False):
        self._count = count
        self.raise_error = raise_error

    def count(self):
        if self.raise_error:
            raise PlaywrightError("boom")
        return self._count


class FakePage:
    """Duck-typed stand-in for playwright's Page: only the methods base.py
    actually calls (locator/count, screenshot, eval_on_selector_all)."""

    def __init__(self, *, locator_counts=None, locator_errors=(), form_state=None,
                 form_state_error=False, screenshot_error=False):
        self._locator_counts = locator_counts or {}
        self._locator_errors = set(locator_errors)
        self._form_state = form_state if form_state is not None else {}
        self._form_state_error = form_state_error
        self._screenshot_error = screenshot_error
        self.screenshot_calls = []

    def locator(self, selector):
        if selector in self._locator_errors:
            return FakeLocator(raise_error=True)
        return FakeLocator(count=self._locator_counts.get(selector, 0))

    def screenshot(self, *, path, full_page=True):
        self.screenshot_calls.append(path)
        if self._screenshot_error:
            raise PlaywrightError("screenshot failed")
        Path(path).write_bytes(b"fake-png-bytes")

    def eval_on_selector_all(self, selector, js):
        if self._form_state_error:
            raise PlaywrightError("eval failed")
        return self._form_state


class WaitablePage(FakePage):
    """FakePage + a no-op wait_for_timeout, and (optionally) a selector whose
    match count clears itself after `clear_after` wait_for_timeout calls --
    for handoff polling tests that need zero real wall-clock waiting.
    `blocked_selector=None` (the default) just means "whatever locator_counts
    says", i.e. FakePage's ordinary behavior."""

    def __init__(self, *, blocked_selector=None, clear_after=None, **kwargs):
        super().__init__(**kwargs)
        self._blocked_selector = blocked_selector
        self._clear_after = clear_after
        self.wait_calls = 0

    def locator(self, selector):
        if self._blocked_selector is not None and selector == self._blocked_selector:
            blocked = self._clear_after is None or self.wait_calls < self._clear_after
            return FakeLocator(count=1 if blocked else 0)
        return super().locator(selector)

    def wait_for_timeout(self, ms):
        self.wait_calls += 1


class FakeFillLocator:
    def __init__(self, count=1, raise_error=False):
        self._count = count
        self.raise_error = raise_error
        self.filled_with = None

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def fill(self, value):
        if self.raise_error:
            raise PlaywrightError("fill failed")
        self.filled_with = value


class FakeFillPage:
    def __init__(self, locator):
        self._locator = locator

    def locator(self, selector):
        return self._locator


class DummyAdapter(PortalAdapter):
    """Minimal real adapter used only to prove the registry+dispatch+evidence
    wiring in run_portal_application -- the actual Ashby/Greenhouse/Lever
    adapters land in later tasks."""

    hosts = ("127.0.0.1",)
    name = "dummy"

    def apply(self, page, entry, package, profile, answers, *, dry_run, client=None, deployment=None,
              handoff=False, notify=None):
        fill_field(page, "#full_name", profile.get("contact", {}).get("name", ""))
        evidence_dir = Path(package["package_dir"]) / "portal"
        return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))


class RaisingAdapter(PortalAdapter):
    """Adapter whose apply() always blows up with a plain exception -- proves
    run_portal_application's generic-failure path degrades to "failed"
    (not needs_human) and still captures evidence at the "dispatch" stage."""

    hosts = ("127.0.0.1",)
    name = "raising"

    def apply(self, page, entry, package, profile, answers, *, dry_run, client=None, deployment=None,
              handoff=False, notify=None):
        raise RuntimeError("adapter exploded")


class TimeoutRaisingAdapter(PortalAdapter):
    """Adapter whose apply() raises a real Playwright TimeoutError -- proves
    run_portal_application maps a mid-dispatch timeout to needs_human
    ("timeout") instead of letting it fall into the generic "failed" branch.
    Raises directly rather than actually waiting so the test stays fast."""

    hosts = ("127.0.0.1",)
    name = "timeout-raising"

    def apply(self, page, entry, package, profile, answers, *, dry_run, client=None, deployment=None,
              handoff=False, notify=None):
        raise PlaywrightTimeoutError("Timeout 5000ms exceeded waiting for selector")


class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """Accepts the connection and the request but never writes a response
    within any test's timeout_s, so page.goto reliably times out client-side
    without relying on a dropped/refused connection (which fails fast with a
    different error, not a timeout)."""

    def do_GET(self):
        time.sleep(2)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>slow</body></html>")

    def log_message(self, *args):
        pass  # keep test output quiet


@contextlib.contextmanager
def _serve_slow():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def clean_registry(monkeypatch):
    """Isolate _REGISTRY per test so DummyAdapter registrations never leak
    into other tests in this module (or later real adapters' own tests)."""
    monkeypatch.setattr(portal_base, "_REGISTRY", [])
    return portal_base._REGISTRY


# --- registry -------------------------------------------------------------

def test_adapter_for_matches_by_host_substring(clean_registry):
    dummy = DummyAdapter()
    register_adapter(dummy)

    found = adapter_for("https://127.0.0.1:8080/jobs/apply/42")

    assert found is dummy


def test_adapter_for_returns_none_when_no_match(clean_registry):
    register_adapter(DummyAdapter())

    assert adapter_for("https://jobs.example.com/apply/42") is None


def test_register_adapter_returns_the_adapter_for_chaining(clean_registry):
    dummy = DummyAdapter()

    result = register_adapter(dummy)

    assert result is dummy
    assert dummy in portal_base._REGISTRY


def test_portal_adapter_base_apply_raises_not_implemented():
    base = PortalAdapter()

    with pytest.raises(NotImplementedError):
        base.apply(None, {}, {}, {}, {}, dry_run=True)


# --- detect_blockers --------------------------------------------------------

def test_detect_blockers_returns_none_on_a_clean_page():
    page = FakePage()

    assert detect_blockers(page) is None


@pytest.mark.parametrize("selector", portal_base._CAPTCHA_SELECTORS)
def test_detect_blockers_returns_captcha_for_each_captcha_selector(selector):
    page = FakePage(locator_counts={selector: 1})

    assert detect_blockers(page) == "captcha"


def test_detect_blockers_returns_login_required_for_password_input():
    page = FakePage(locator_counts={"input[type='password']": 1})

    assert detect_blockers(page) == "login-required"


def test_detect_blockers_prefers_captcha_over_login_when_both_present():
    page = FakePage(locator_counts={
        portal_base._CAPTCHA_SELECTORS[0]: 1,
        "input[type='password']": 1,
    })

    assert detect_blockers(page) == "captcha"


def test_detect_blockers_swallows_locator_errors_and_keeps_checking():
    page = FakePage(
        locator_errors={portal_base._CAPTCHA_SELECTORS[0]},
        locator_counts={"input[type='password']": 1},
    )

    assert detect_blockers(page) == "login-required"


# --- handoff: wait_for_blocker_clear / resolve_blocker -----------------------

def test_wait_for_blocker_clear_returns_true_after_n_polls():
    page = WaitablePage(blocked_selector=portal_base._CAPTCHA_SELECTORS[0], clear_after=2)
    notified = []

    result = wait_for_blocker_clear(page, timeout_s=5, notify=notified.append)

    assert result is True
    assert page.wait_calls == 2
    assert notified == ["captcha waiting in the browser on the mini"]


def test_wait_for_blocker_clear_returns_false_on_timeout():
    page = WaitablePage(blocked_selector=portal_base._CAPTCHA_SELECTORS[0], clear_after=None)

    result = wait_for_blocker_clear(page, timeout_s=0, notify=None)

    assert result is False


def test_wait_for_blocker_clear_notify_failure_is_swallowed():
    page = WaitablePage(blocked_selector=portal_base._CAPTCHA_SELECTORS[0], clear_after=1)

    def boom(_msg):
        raise RuntimeError("telegram down")

    result = wait_for_blocker_clear(page, timeout_s=5, notify=boom)  # must not raise

    assert result is True


def test_resolve_blocker_handoff_cleared_returns_none_to_continue_flow():
    page = WaitablePage()  # no blocker selector configured -> already clear

    result = resolve_blocker(page, "captcha", "unused-evidence-dir", stage="captcha",
                              handoff=True, notify=None)

    assert result is None


def test_resolve_blocker_handoff_timeout_returns_needs_human(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "0")
    page = WaitablePage(blocked_selector=portal_base._CAPTCHA_SELECTORS[0], clear_after=None)
    evidence_dir = tmp_path / "portal"

    result = resolve_blocker(page, "captcha", evidence_dir, stage="captcha",
                              handoff=True, notify=None)

    assert result.status == "needs_human"
    assert result.reason == "handoff-timeout: captcha not solved"
    assert (evidence_dir / "aborted.png").exists()


def test_resolve_blocker_non_handoff_returns_immediate_needs_human(tmp_path):
    page = FakePage()
    evidence_dir = tmp_path / "portal"

    result = resolve_blocker(page, "login-required", evidence_dir, stage="login-required",
                              handoff=False, notify=None)

    assert result.status == "needs_human"
    assert result.reason == "login-required"
    assert (evidence_dir / "login-required.png").exists()


# --- capture_evidence --------------------------------------------------------

def test_capture_evidence_writes_screenshot_and_form_state(tmp_path):
    page = FakePage(form_state={"full_name": "Teodor", "email": "t@example.com"})
    evidence_dir = tmp_path / "portal"

    capture_evidence(page, evidence_dir, "filled")

    png = evidence_dir / "filled.png"
    state_file = evidence_dir / "form_state.json"
    assert png.exists()
    assert json.loads(state_file.read_text()) == {"full_name": "Teodor", "email": "t@example.com"}


def test_capture_evidence_creates_missing_evidence_dir(tmp_path):
    page = FakePage()
    evidence_dir = tmp_path / "nested" / "portal"
    assert not evidence_dir.exists()

    capture_evidence(page, evidence_dir, "start")

    assert evidence_dir.is_dir()


def test_capture_evidence_second_call_overwrites_form_state_but_keeps_both_screenshots(tmp_path):
    evidence_dir = tmp_path / "portal"
    page1 = FakePage(form_state={"full_name": "old"})
    capture_evidence(page1, evidence_dir, "start")
    page2 = FakePage(form_state={"full_name": "new"})

    capture_evidence(page2, evidence_dir, "filled")

    assert (evidence_dir / "start.png").exists()
    assert (evidence_dir / "filled.png").exists()
    assert json.loads((evidence_dir / "form_state.json").read_text()) == {"full_name": "new"}


def test_capture_evidence_form_state_empty_dict_on_eval_error(tmp_path):
    page = FakePage(form_state_error=True)
    evidence_dir = tmp_path / "portal"

    capture_evidence(page, evidence_dir, "start")

    assert json.loads((evidence_dir / "form_state.json").read_text()) == {}


def test_capture_evidence_does_not_raise_when_screenshot_fails(tmp_path):
    page = FakePage(screenshot_error=True, form_state={"full_name": "Teodor"})
    evidence_dir = tmp_path / "portal"

    capture_evidence(page, evidence_dir, "start")

    assert not (evidence_dir / "start.png").exists()
    assert json.loads((evidence_dir / "form_state.json").read_text()) == {"full_name": "Teodor"}


def test_capture_evidence_does_not_raise_when_mkdir_fails(tmp_path):
    # evidence_dir's parent is a FILE, not a directory, so
    # `evidence_dir.mkdir(parents=True)` raises NotADirectoryError.
    blocker_file = tmp_path / "blocker"
    blocker_file.write_text("not a directory")
    evidence_dir = blocker_file / "portal"
    page = FakePage(form_state={"full_name": "Teodor"})

    capture_evidence(page, evidence_dir, "start")  # must not raise

    assert not evidence_dir.exists()


def test_capture_evidence_does_not_raise_when_form_state_write_fails(tmp_path, monkeypatch):
    page = FakePage(form_state={"full_name": "Teodor"})
    evidence_dir = tmp_path / "portal"
    original_write_text = Path.write_text

    def flaky_write_text(self, *args, **kwargs):
        if self.name == "form_state.json":
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", flaky_write_text)

    capture_evidence(page, evidence_dir, "start")  # must not raise

    # Screenshot happened before the write_text failure, so it's still there;
    # the form_state write itself never landed.
    assert (evidence_dir / "start.png").exists()
    assert not (evidence_dir / "form_state.json").exists()


# --- fill_field ---------------------------------------------------------------

def test_fill_field_returns_false_for_empty_value():
    page = FakeFillPage(FakeFillLocator())

    assert fill_field(page, "#full_name", "") is False


def test_fill_field_returns_false_when_element_missing():
    page = FakeFillPage(FakeFillLocator(count=0))

    assert fill_field(page, "#full_name", "Teodor") is False


def test_fill_field_fills_and_returns_true_on_success():
    locator = FakeFillLocator(count=1)
    page = FakeFillPage(locator)

    result = fill_field(page, "#full_name", "Teodor Lutoiu")

    assert result is True
    assert locator.filled_with == "Teodor Lutoiu"


def test_fill_field_returns_false_on_playwright_error():
    page = FakeFillPage(FakeFillLocator(count=1, raise_error=True))

    assert fill_field(page, "#full_name", "Teodor") is False


def test_fill_field_coerces_non_string_values():
    locator = FakeFillLocator(count=1)
    page = FakeFillPage(locator)

    fill_field(page, "#years", 5)

    assert locator.filled_with == "5"


# --- verify_filled / verify_file_attached (real Playwright) ------------------

def test_verify_filled_reads_the_value_back_true_and_normalizes_case():
    from playwright.sync_api import sync_playwright

    with serve_fixtures() as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/simple_form.html", wait_until="load")
            fill_field(page, "#full_name", "Teodor Lutoiu")

            assert verify_filled(page, "#full_name", "Teodor Lutoiu") is True
            # case- and whitespace-normalized
            assert verify_filled(page, "#full_name", "  teodor lutoiu ") is True
        finally:
            browser.close()


def test_verify_filled_false_on_mismatch_missing_and_empty_expected():
    from playwright.sync_api import sync_playwright

    with serve_fixtures() as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/simple_form.html", wait_until="load")
            fill_field(page, "#full_name", "Teodor Lutoiu")

            assert verify_filled(page, "#full_name", "Someone Else") is False
            assert verify_filled(page, "#does_not_exist", "x") is False
            assert verify_filled(page, "#full_name", "") is False
        finally:
            browser.close()


def test_verify_file_attached_true_only_after_a_file_is_set(tmp_path):
    from playwright.sync_api import sync_playwright

    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 fake\n")

    with serve_fixtures() as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/simple_form.html", wait_until="load")

            # Nothing attached yet, and a missing selector is False, never raises.
            assert verify_file_attached(page, "#resume") is False
            assert verify_file_attached(page, "#no_such_input") is False

            page.locator("#resume").set_input_files(str(cv))
            assert verify_file_attached(page, "#resume") is True
        finally:
            browser.close()


# --- run_portal_application: pre-browser branches ----------------------------

def test_run_portal_application_needs_human_when_apply_target_missing(tmp_path, clean_registry):
    package = {"package_dir": str(tmp_path / "pkg")}
    entry = {"id": "job-1"}

    result = run_portal_application(entry, package, {}, {}, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "missing-apply-target"
    assert result.evidence_dir == str(tmp_path / "pkg" / "portal")


def test_run_portal_application_needs_human_when_no_adapter_registered(tmp_path, clean_registry):
    package = {"package_dir": str(tmp_path / "pkg")}
    entry = {"id": "job-1", "apply_target": "https://jobs.unknown-ats.example/42"}

    result = run_portal_application(entry, package, {}, {}, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "no-adapter"


def test_run_portal_application_falls_back_to_url_when_apply_target_absent(tmp_path, clean_registry):
    package = {"package_dir": str(tmp_path / "pkg")}
    entry = {"id": "job-1", "url": "https://jobs.unknown-ats.example/42"}

    result = run_portal_application(entry, package, {}, {}, dry_run=True)

    # Reaches the adapter lookup (not the missing-apply-target branch), and
    # since no adapter claims this host, degrades the same way.
    assert result.status == "needs_human"
    assert result.reason == "no-adapter"


# --- real-Playwright smoke tests (unmarked: fast, local-only) ----------------

def test_smoke_navigate_fill_and_capture_evidence(tmp_path):
    from playwright.sync_api import sync_playwright

    evidence_dir = tmp_path / "portal"
    with serve_fixtures() as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/simple_form.html", wait_until="load")

            assert fill_field(page, "#full_name", "Teodor Lutoiu") is True
            assert fill_field(page, "#email", "contact@teodorlutoiu.com") is True

            capture_evidence(page, evidence_dir, "filled")
        finally:
            browser.close()

    assert (evidence_dir / "filled.png").exists()
    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["full_name"] == "Teodor Lutoiu"
    assert state["email"] == "contact@teodorlutoiu.com"


def test_smoke_detect_blockers_captcha_on_real_page():
    from playwright.sync_api import sync_playwright

    with serve_fixtures() as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/blocked_form.html", wait_until="load")

            assert detect_blockers(page) == "captcha"
        finally:
            browser.close()


def test_smoke_run_portal_application_dispatches_to_adapter_end_to_end(tmp_path, clean_registry):
    register_adapter(DummyAdapter())
    profile = {"contact": {"name": "Teodor Lutoiu"}}
    package = {"package_dir": str(tmp_path / "pkg")}

    with serve_fixtures() as base_url:
        entry = {"id": "job-1", "apply_target": f"{base_url}/simple_form.html"}
        result = run_portal_application(entry, package, profile, {}, dry_run=True)

    assert result.status == "filled"
    assert result.reason == ""
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "filled.png").exists()
    state = json.loads((evidence_dir / "form_state.json").read_text())
    assert state["full_name"] == "Teodor Lutoiu"


# --- run_portal_application: browser-backed failure paths --------------------

class ClientCapturingAdapter(PortalAdapter):
    """Records whatever client/deployment run_portal_application forwarded,
    proving the C6 client-threading wire-up reaches the adapter."""

    hosts = ("127.0.0.1",)
    name = "client-capturing"

    def __init__(self):
        self.seen_client = "unset"
        self.seen_deployment = "unset"

    def apply(self, page, entry, package, profile, answers, *, dry_run, client=None, deployment=None,
              handoff=False, notify=None):
        self.seen_client = client
        self.seen_deployment = deployment
        evidence_dir = Path(package["package_dir"]) / "portal"
        return PortalResult(status="filled", reason="", evidence_dir=str(evidence_dir))


def test_run_portal_application_forwards_client_and_deployment_to_adapter(tmp_path, clean_registry):
    adapter = ClientCapturingAdapter()
    register_adapter(adapter)
    package = {"package_dir": str(tmp_path / "pkg")}
    sentinel_client = object()

    with serve_fixtures() as base_url:
        entry = {"id": "job-1", "apply_target": f"{base_url}/simple_form.html"}
        run_portal_application(entry, package, {}, {}, dry_run=True,
                                client=sentinel_client, deployment="gpt-4o-mini")

    assert adapter.seen_client is sentinel_client
    assert adapter.seen_deployment == "gpt-4o-mini"


def test_run_portal_application_needs_human_when_blocker_detected_mid_navigation(tmp_path, clean_registry):
    register_adapter(DummyAdapter())
    package = {"package_dir": str(tmp_path / "pkg")}

    with serve_fixtures() as base_url:
        entry = {"id": "job-1", "apply_target": f"{base_url}/blocked_form.html"}
        result = run_portal_application(entry, package, {}, {}, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "captcha"
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "captcha.png").exists()
    assert (evidence_dir / "form_state.json").exists()


def test_run_portal_application_failed_when_adapter_raises(tmp_path, clean_registry):
    register_adapter(RaisingAdapter())
    package = {"package_dir": str(tmp_path / "pkg")}

    with serve_fixtures() as base_url:
        entry = {"id": "job-1", "apply_target": f"{base_url}/simple_form.html"}
        result = run_portal_application(entry, package, {}, {}, dry_run=True)

    assert result.status == "failed"
    assert "adapter exploded" in result.reason
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "dispatch.png").exists()
    assert (evidence_dir / "form_state.json").exists()


def test_run_portal_application_needs_human_timeout_when_adapter_raises_playwright_timeout(tmp_path, clean_registry):
    register_adapter(TimeoutRaisingAdapter())
    package = {"package_dir": str(tmp_path / "pkg")}

    with serve_fixtures() as base_url:
        entry = {"id": "job-1", "apply_target": f"{base_url}/simple_form.html"}
        result = run_portal_application(entry, package, {}, {}, dry_run=True)

    assert result.status == "needs_human"
    assert result.reason == "timeout"
    evidence_dir = Path(result.evidence_dir)
    assert (evidence_dir / "timeout.png").exists()
    assert (evidence_dir / "form_state.json").exists()


@pytest.mark.integration
def test_run_portal_application_needs_human_timeout_on_slow_navigation(tmp_path, clean_registry):
    register_adapter(DummyAdapter())
    package = {"package_dir": str(tmp_path / "pkg")}

    with _serve_slow() as base_url:
        entry = {"id": "job-1", "apply_target": f"{base_url}/anything"}
        result = run_portal_application(entry, package, {}, {}, dry_run=True, timeout_s=1)

    assert result.status == "needs_human"
    assert result.reason == "timeout"
    # The attempt happened -- evidence dir + form_state.json always land,
    # even though the screenshot may be of a still-loading blank page.
    evidence_dir = Path(result.evidence_dir)
    assert evidence_dir.is_dir()
    assert (evidence_dir / "form_state.json").exists()


def test_run_portal_application_handoff_waits_on_predispatch_blocker(monkeypatch, tmp_path):
    """A blocker visible right at page load must engage the handoff wait, not
    abort -- the live wire-test bug: base's pre-dispatch check ignored handoff."""
    import cv_tailor.portal.base as base

    calls = {"n": 0}

    def fake_detect(page):
        calls["n"] += 1
        return "captcha" if calls["n"] <= 2 else None  # clears on 3rd poll

    monkeypatch.setattr(base, "detect_blockers", fake_detect)
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "10")

    dispatched = {}

    class DummyAdapter(base.PortalAdapter):
        hosts = ("example-handoff.test",)
        name = "dummy-handoff"

        def apply(self, page, entry, package, profile, answers, *, dry_run,
                  client=None, deployment=None, handoff=False, notify=None):
            dispatched["yes"] = True
            return base.PortalResult("filled", "", str(tmp_path))

    monkeypatch.setattr(base, "_REGISTRY", [DummyAdapter()])
    monkeypatch.setattr(base.time, "sleep", lambda s: None)

    class FakePage:
        url = "https://example-handoff.test/job"
        def goto(self, *a, **k): pass
        def set_default_timeout(self, *a, **k): pass
        def screenshot(self, *a, **k): pass
        def evaluate(self, *a, **k): return {}
        def wait_for_timeout(self, ms): pass

    class FakeCtx:
        def new_page(self): return FakePage()
        def close(self): pass

    class FakeBrowser:
        def new_context(self, **k): return FakeCtx()
        def new_page(self): return FakePage()
        def close(self): pass

    class FakeChromium:
        def launch(self, **k): return FakeBrowser()

    class FakePW:
        chromium = FakeChromium()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(base, "sync_playwright", lambda: FakePW())

    entry = {"apply_target": "https://example-handoff.test/job", "id": "x", "company": "X", "title": "T"}
    package = {"package_dir": str(tmp_path), "cv_path": str(tmp_path / "cv.pdf"),
               "cover_letter_path": str(tmp_path / "cl.md")}
    result = base.run_portal_application(entry, package, {}, {}, dry_run=True, handoff=True)
    assert dispatched.get("yes"), "adapter must be dispatched after the blocker clears"
    assert result.status == "filled"
