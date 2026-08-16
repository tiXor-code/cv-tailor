"""'Finish in browser' has to actually finish in a browser. Before this, a
handoff on a host no adapter claims hit the same adapter_for gate as the
unattended run and returned no-adapter without ever opening anything -- the
button was a no-op on exactly the jobs it existed for."""
from playwright.sync_api import Error as PlaywrightError

import cv_tailor.portal.base as base
from cv_tailor.portal.base import PortalResult, run_portal_application


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
    page, record = FakePage(closed_after=10**9), {}
    _patch(monkeypatch, page, record)
    monkeypatch.setenv("APPLY_HANDOFF_TIMEOUT", "0")
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
