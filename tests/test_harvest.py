# tests/test_harvest.py
"""Ashby board harvester: turn companies the scan has ALREADY seen into
standing, keyless sources on the one ATS whose portal adapter completes
end-to-end.

Measured 2026-09-16 against the live board API: of 45 companies that had
ever scored >= 6, ten have a live Ashby board carrying 838 open roles
between them -- against the five ashby slugs sources.yaml configures today.
"""
import io
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cv_tailor import harvest


def _fake_urlopen(payload):
    buf = io.BytesIO(json.dumps(payload).encode())
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    return cm


def _answers(payload):
    return lambda *a, **kw: _fake_urlopen(payload)


def test_an_empty_board_is_not_a_valid_slug():
    """The trap this rule exists for: a slug with no board at all answers
    HTTP 200 with jobs: [] (verified live on 'vercel'), which is
    indistinguishable from success if validity is read off the status code.
    Validity is len(jobs) > 0, never the HTTP status."""
    with mock.patch("urllib.request.urlopen", side_effect=_answers({"jobs": []})):
        assert harvest.validate_ashby_slug("vercel") is False


def test_a_board_with_open_roles_is_a_valid_slug():
    payload = {"jobs": [{"id": "1", "title": "Forward Deployed Engineer"}]}
    with mock.patch("urllib.request.urlopen", side_effect=_answers(payload)):
        assert harvest.validate_ashby_slug("checkly") is True


def test_a_failed_probe_is_never_treated_as_a_valid_slug():
    """A timeout says nothing about whether the board exists. Fail closed:
    an unreachable probe must not enrol a source."""
    with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        assert harvest.validate_ashby_slug("checkly") is False
