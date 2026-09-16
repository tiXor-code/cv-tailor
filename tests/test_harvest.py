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


# --- enrolment ------------------------------------------------------------------
#
# Harvested slugs auto-enrol: no review queue (Teodor, 2026-08-17). That makes
# three properties load-bearing, so each has a test below: enrolment is
# idempotent, a missing file is not an error, and there is a de-enrol path that
# is not "hand-edit a generated file the next harvest will simply re-add".
#
# The file is generated, changes daily and lives under data/ (already
# gitignored as runtime state), so it is deliberately NOT sources.yaml: that
# one is hand-curated and carries measured notes a machine rewrite would
# destroy.

def test_enrolment_writes_entries_fetch_all_understands(tmp_path):
    path = tmp_path / "sources_harvested.yaml"

    added = harvest.enrol_ashby_slugs(path, ["checkly", "camunda"])

    assert added == ["checkly", "camunda"]
    assert harvest.load_harvested_sources(path) == [
        {"kind": "ashby", "slug": "checkly", "name": "checkly"},
        {"kind": "ashby", "slug": "camunda", "name": "camunda"},
    ]


def test_enrolling_the_same_slug_twice_never_duplicates_it(tmp_path):
    path = tmp_path / "sources_harvested.yaml"
    harvest.enrol_ashby_slugs(path, ["checkly"])

    assert harvest.enrol_ashby_slugs(path, ["checkly"]) == []
    assert len(harvest.load_harvested_sources(path)) == 1


def test_a_missing_harvest_file_is_not_an_error(tmp_path):
    """The scan must run normally on a machine that has never harvested."""
    assert harvest.load_harvested_sources(tmp_path / "never_written.yaml") == []


def test_a_disabled_slug_is_dropped_and_never_re_enrolled(tmp_path):
    """The registry must not be a one-way ratchet: a bad board has to be
    removable without hand-editing, and the next harvest must not undo it."""
    path = tmp_path / "sources_harvested.yaml"
    harvest.enrol_ashby_slugs(path, ["badboard"])

    harvest.disable_ashby_slug(path, "badboard")

    assert harvest.load_harvested_sources(path) == []
    assert harvest.enrol_ashby_slugs(path, ["badboard"]) == []


# --- one pass: probe, enrol, remember -------------------------------------------

def test_harvest_and_enrol_adds_the_boards_it_finds(tmp_path):
    path = tmp_path / "sources_harvested.yaml"
    payload = {"jobs": [{"id": "1", "title": "Engineer"}]}

    with mock.patch("urllib.request.urlopen", side_effect=_answers(payload)):
        added = harvest.harvest_and_enrol(path, ["Checkly"])

    assert added == ["checkly"]
    assert harvest.load_harvested_sources(path) == [
        {"kind": "ashby", "slug": "checkly", "name": "checkly"}]


def test_a_slug_probed_in_an_earlier_run_is_never_probed_again(tmp_path):
    """The pool is every company ever seen (1,435 today) and this runs every
    morning. Without a memory of what has already been probed, each run would
    re-ask someone else's API thousands of questions it already knows the
    answer to -- and a company with no board is the common case, so the
    no-board answer is exactly the one worth remembering."""
    path = tmp_path / "sources_harvested.yaml"
    calls = []

    def _count(*a, **kw):
        calls.append(1)
        return _fake_urlopen({"jobs": []})

    with mock.patch("urllib.request.urlopen", side_effect=_count):
        harvest.harvest_and_enrol(path, ["Nobody Inc"])
        first = len(calls)
        harvest.harvest_and_enrol(path, ["Nobody Inc"])

    assert first > 0, "the first run must actually probe"
    assert len(calls) == first, "the second run must probe nothing"


def test_the_number_of_probes_in_one_run_is_capped(tmp_path):
    """Day one faces the whole backlog. The cap keeps a single morning's run
    bounded and polite; the probe memory means the backlog still drains."""
    path = tmp_path / "sources_harvested.yaml"
    calls = []

    def _count(*a, **kw):
        calls.append(1)
        return _fake_urlopen({"jobs": []})

    companies = [f"Company {i}" for i in range(10)]
    with mock.patch("urllib.request.urlopen", side_effect=_count):
        harvest.harvest_and_enrol(path, companies, limit=3)

    assert len(calls) == 3
