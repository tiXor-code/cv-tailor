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


# --- turning seen companies into slugs -----------------------------------------

def test_harvest_returns_the_validated_slug_for_a_company_name():
    payload = {"jobs": [{"id": "1", "title": "Forward Deployed Engineer"}]}
    with mock.patch("urllib.request.urlopen", side_effect=_answers(payload)):
        found = harvest.harvest_ashby_slugs(["Checkly"])

    assert found == ["checkly"]


def test_harvest_never_re_enrols_a_slug_already_configured():
    """Enrolment must be idempotent: a slug enrolling twice, or silently
    replacing an existing entry, has to be impossible. Comparison is
    case-folded because sources.yaml carries 'Deel' while the derived
    candidate is 'deel'."""
    payload = {"jobs": [{"id": "1", "title": "Engineer"}]}
    with mock.patch("urllib.request.urlopen", side_effect=_answers(payload)):
        found = harvest.harvest_ashby_slugs(["Deel"], known_slugs=["Deel"])

    assert found == []


def test_harvest_collapses_two_spellings_of_the_same_company():
    """Real duplicate from the live probe: seen_jobs carries both 'Camunda'
    and 'camunda', and both normalise to the same board."""
    payload = {"jobs": [{"id": "1", "title": "Engineer"}]}
    with mock.patch("urllib.request.urlopen", side_effect=_answers(payload)):
        found = harvest.harvest_ashby_slugs(["Camunda", "camunda"])

    assert found == ["camunda"]


def test_harvest_skips_an_aggregator_even_when_it_has_a_board():
    """An aggregator is not an employer. Enrolling one pours thousands of
    aggregator-quality postings into a scan that sees ~11 new postings a day,
    burning scoring spend on jobs that never clear the floor. The board API
    cannot tell us this -- the deny list is the only guard."""
    payload = {"jobs": [{"id": "1", "title": "Engineer"}]}
    with mock.patch("urllib.request.urlopen", side_effect=_answers(payload)):
        found = harvest.harvest_ashby_slugs(["Jobgether"])

    assert found == []


def test_harvest_never_probes_the_same_slug_twice():
    """One probe per candidate slug: the pool is every company ever seen
    (1,435 of them), so a duplicate probe is a duplicate HTTP request against
    someone else's API."""
    payload = {"jobs": [{"id": "1", "title": "Engineer"}]}
    calls = []

    def _count(*a, **kw):
        calls.append(getattr(a[0], "full_url", str(a[0])))
        return _fake_urlopen(payload)

    with mock.patch("urllib.request.urlopen", side_effect=_count):
        harvest.harvest_ashby_slugs(["Camunda", "camunda", "Camunda"])

    assert len(calls) == 1
