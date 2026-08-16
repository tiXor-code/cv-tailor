"""'Finish in browser' has to actually finish in a browser. Before this, a
handoff on a host no adapter claims hit the same adapter_for gate as the
unattended run and returned no-adapter without ever opening anything -- the
button was a no-op on exactly the jobs it existed for."""
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

import cv_tailor.portal.base as base
from cv_tailor.portal.base import run_portal_application


class FakePage:
    def __init__(self, closed_after=0):
        self.goto_urls = []
        self.default_timeouts = []
        self._closed_after = closed_after
        self._is_closed_calls = 0
        self.screenshots = []

    def set_default_timeout(self, ms):
        self.default_timeouts.append(ms)

    def goto(self, url, **kw):
        self.goto_urls.append(url)

    def is_closed(self):
        self._is_closed_calls += 1
        return self._is_closed_calls > self._closed_after

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, path=None, full_page=False):
        self.screenshots.append(path)

    def eval_on_selector_all(self, sel, js):
        return {}


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False

    def new_page(self):
        return self._page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, page, record):
        self._page = page
        self._record = record

    def launch(self, headless=True):
        self._record["headless"] = headless
        return FakeBrowser(self._page)


class FakePlaywright:
    def __init__(self, page, record):
        self.chromium = FakeChromium(page, record)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch(monkeypatch, page, record):
    monkeypatch.setattr(base, "sync_playwright", lambda: FakePlaywright(page, record))
    monkeypatch.setattr(base, "detect_blockers", lambda p: None)


def _entry():
    return {"apply_target": "https://www.google.com/search?ibp=htl;jobs&q=x"}


def _package(tmp_path):
    return {"package_dir": str(tmp_path)}


def test_unattended_no_adapter_never_launches_a_browser(monkeypatch, tmp_path):
    def _boom():
        raise AssertionError("browser launched on an unattended no-adapter run")

    monkeypatch.setattr(base, "sync_playwright", _boom)
    result = run_portal_application(_entry(), _package(tmp_path), {}, {},
                                    dry_run=False, handoff=False)
    assert result.status == "needs_human"
    assert result.reason == "no-adapter"


def test_handoff_no_adapter_opens_a_headed_browser_at_the_url(monkeypatch, tmp_path):
    page, record = FakePage(closed_after=1), {}
    _patch(monkeypatch, page, record)
    result = run_portal_application(_entry(), _package(tmp_path), {}, {},
                                    dry_run=False, handoff=True)
    assert record["headless"] is False
    assert page.goto_urls == ["https://www.google.com/search?ibp=htl;jobs&q=x"]
    assert result.status == "needs_human"
    assert result.reason == "handoff-manual: no adapter"


def test_handoff_no_adapter_notifies_the_human_once(monkeypatch, tmp_path):
    page, record = FakePage(closed_after=1), {}
    _patch(monkeypatch, page, record)
    sent = []
    run_portal_application(_entry(), _package(tmp_path), {}, {},
                           dry_run=False, handoff=True, notify=sent.append)
    assert len(sent) == 1
    assert "browser" in sent[0].lower()


def test_handoff_no_adapter_captures_evidence(monkeypatch, tmp_path):
    page, record = FakePage(closed_after=1), {}
    _patch(monkeypatch, page, record)
    result = run_portal_application(_entry(), _package(tmp_path), {}, {},
                                    dry_run=False, handoff=True)
    assert result.evidence_dir == str(tmp_path / "portal")
    assert any(str(p).endswith("handoff.png") for p in page.screenshots)


def test_handoff_no_adapter_times_out_without_a_close(monkeypatch, tmp_path):
    # APPLY_MANUAL_HANDOFF_TIMEOUT, not APPLY_HANDOFF_TIMEOUT: this path is
    # governed by the manual budget now (see the block at the bottom of this
    # file), and the fake page never closes, so only that knob ends the wait.
    page, record = FakePage(closed_after=10**9), {}
    _patch(monkeypatch, page, record)
    monkeypatch.setenv("APPLY_MANUAL_HANDOFF_TIMEOUT", "0")
    result = run_portal_application(_entry(), _package(tmp_path), {}, {},
                                    dry_run=False, handoff=True)
    assert result.status == "needs_human"
    assert result.reason == "handoff-manual: no adapter"


def test_missing_apply_target_still_short_circuits_in_handoff(monkeypatch, tmp_path):
    def _boom():
        raise AssertionError("browser launched with no URL to open")

    monkeypatch.setattr(base, "sync_playwright", _boom)
    result = run_portal_application({}, _package(tmp_path), {}, {},
                                    dry_run=False, handoff=True)
    assert result.reason == "missing-apply-target"


# --- the handoff branch runs BEFORE the blocker check ------------------------

def test_handoff_no_adapter_never_consults_detect_blockers(monkeypatch, tmp_path):
    """detect_blockers exists to stop an AUTOMATED fill from crashing into a
    wall. On the no-adapter path nothing is automated, so a captcha or login is
    the human's to solve. Consulting it here is actively harmful: _LOGIN_SELECTOR
    matches ANY password input (a header login box, a footer form), so the run
    would burn the whole handoff timeout waiting for a human to 'clear'
    something that was never in the way, then return 'handoff-timeout: captcha
    not solved' -- which IS in _NO_SUBMIT_REASONS, deleting the ledger row for
    an application the human may well have submitted."""
    page, record = FakePage(closed_after=1), {}
    monkeypatch.setattr(base, "sync_playwright", lambda: FakePlaywright(page, record))
    seen = []
    monkeypatch.setattr(base, "detect_blockers",
                        lambda p: seen.append(p) or "login-required")
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "0")

    result = run_portal_application(_entry(), _package(tmp_path), {}, {},
                                    dry_run=False, handoff=True)

    assert seen == [], "the no-adapter handoff must return before the blocker check"
    assert result.reason == "handoff-manual: no adapter"


# --- evidence is captured before the wait, not only in the finally -----------

class ClosingPage(FakePage):
    """The real happy path: the human closes the tab, and every later call
    against it raises the way playwright does. Writes real bytes while open, so
    whether the file exists proves WHEN the screenshot was taken."""

    def __init__(self, closed_after=1):
        super().__init__(closed_after=closed_after)
        self.closed = False

    def is_closed(self):
        result = super().is_closed()
        if result:
            self.closed = True
        return result

    def screenshot(self, path=None, full_page=False):
        if self.closed:
            raise PlaywrightError("Target page, context or browser has been closed")
        super().screenshot(path)
        Path(path).write_bytes(b"fake-png-bytes")

    def eval_on_selector_all(self, sel, js):
        if self.closed:
            raise PlaywrightError("Target page, context or browser has been closed")
        return {}


def test_handoff_png_survives_the_human_closing_the_tab(monkeypatch, tmp_path):
    """The artifact must exist exactly when the run WENT WELL. The finally's
    capture runs after the human closed the tab, so its screenshot raises --
    only a capture taken before the wait leaves a file behind."""
    page, record = ClosingPage(closed_after=1), {}
    _patch(monkeypatch, page, record)

    result = run_portal_application(_entry(), _package(tmp_path), {}, {},
                                    dry_run=False, handoff=True)

    assert result.reason == "handoff-manual: no adapter"
    assert (tmp_path / "portal" / "handoff.png").read_bytes() == b"fake-png-bytes"


# --- non-http(s) apply targets never reach the browser -----------------------

@pytest.mark.parametrize("bad_url", [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "ftp://example.com/job",
    "data:text/html,<h1>x</h1>",
])
def test_non_http_apply_target_never_reaches_the_browser(monkeypatch, tmp_path, bad_url):
    """Handoff is the first path that ever navigates to a host no adapter
    claims, and apply_target comes from an untrusted feed -- so the URL guard
    has to be fail-closed on the scheme, not just on emptiness."""
    def _boom():
        raise AssertionError(f"browser launched for a non-http(s) target: {bad_url}")

    monkeypatch.setattr(base, "sync_playwright", _boom)

    result = run_portal_application({"apply_target": bad_url}, _package(tmp_path), {}, {},
                                    dry_run=False, handoff=True)

    assert result.status == "needs_human"
    assert result.reason == "missing-apply-target"


# --- wait_for_human_close error branches -------------------------------------
#
# The helper holds a real browser open around a human. Every way that browser
# can die on it must end in a return, never an exception: run_portal_application
# wraps this call in a blanket `except Exception` that would relabel the result
# "failed", and apply_approved rolls a "failed" attempt's ledger row back --
# which is exactly wrong after a human may have submitted by hand.

class DeadPage(FakePage):
    """is_closed() raises the way playwright does once the tab/browser is gone,
    instead of politely returning True."""

    def is_closed(self):
        raise PlaywrightError("Target page, context or browser has been closed")


class UnwaitablePage(FakePage):
    """Still open, but the poll itself blows up (browser died mid-wait)."""

    def wait_for_timeout(self, ms):
        raise PlaywrightError("Target closed")


def test_wait_for_human_close_treats_a_dead_page_as_closed():
    assert base.wait_for_human_close(DeadPage(), timeout_s=600) is True


def test_wait_for_human_close_returns_false_when_the_poll_dies():
    # Open forever, so only the failing wait can end the loop.
    assert base.wait_for_human_close(UnwaitablePage(closed_after=10**9),
                                     timeout_s=600) is False


def test_wait_for_human_close_survives_a_notify_that_raises():
    def boom(_msg):
        raise RuntimeError("telegram down")

    assert base.wait_for_human_close(FakePage(closed_after=0), timeout_s=600,
                                     notify=boom) is True


# --- the manual handoff gets its own budget ----------------------------------
#
# This wait is not a CAPTCHA wait. handoff_timeout_s's 600s covers "a human
# solves a challenge on a form the adapter already filled". Here the human does
# the ENTIRE application by hand -- find the posting, fill it, upload a CV. At
# 600s wait_for_human_close returns False, the finally runs, and browser.close()
# pulls the window out from under someone mid-form. APPLY_HANDOFF_TIMEOUT is not
# set in the live .env, so that 600s was the value actually in force.

def test_manual_handoff_timeout_defaults_to_an_hour(monkeypatch):
    monkeypatch.delenv("APPLY_MANUAL_HANDOFF_TIMEOUT", raising=False)
    assert base.manual_handoff_timeout_s() == 3600.0


def test_manual_handoff_timeout_reads_the_env_fresh_every_call(monkeypatch):
    # Never cached at import: the value must follow the environment within a
    # single process, the same contract handoff_timeout_s has.
    monkeypatch.setenv("APPLY_MANUAL_HANDOFF_TIMEOUT", "120")
    assert base.manual_handoff_timeout_s() == 120.0
    monkeypatch.setenv("APPLY_MANUAL_HANDOFF_TIMEOUT", "240")
    assert base.manual_handoff_timeout_s() == 240.0


@pytest.mark.parametrize("bad", ["", "abc", "10 minutes", "  "])
def test_manual_handoff_timeout_falls_back_on_a_malformed_value(monkeypatch, bad):
    # A typo in the env must not degrade to 0 -- that would close the browser
    # instantly on every manual handoff. Fail back to the sane default.
    monkeypatch.setenv("APPLY_MANUAL_HANDOFF_TIMEOUT", bad)
    assert base.manual_handoff_timeout_s() == 3600.0


def test_captcha_budget_is_untouched(monkeypatch):
    """handoff_timeout_s still governs blocker waits and adapter submit waits
    at 600s -- the manual budget is additive, not a redefinition."""
    monkeypatch.delenv("APPLY_HANDOFF_TIMEOUT", raising=False)
    assert base.handoff_timeout_s() == 600.0


def test_manual_handoff_call_site_uses_the_manual_budget(monkeypatch, tmp_path):
    """Pins WHICH budget reaches wait_for_human_close. Set the two knobs to
    different values: if the call site ever reverts to handoff_timeout_s, this
    reads 5 instead of 1234."""
    page, record = FakePage(closed_after=10**9), {}
    _patch(monkeypatch, page, record)
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "5")
    monkeypatch.setenv("APPLY_MANUAL_HANDOFF_TIMEOUT", "1234")
    seen = []
    monkeypatch.setattr(base, "wait_for_human_close",
                        lambda page, timeout_s, notify=None: seen.append(timeout_s) or True)

    run_portal_application(_entry(), _package(tmp_path), {}, {},
                           dry_run=False, handoff=True)

    assert seen == [1234.0]
