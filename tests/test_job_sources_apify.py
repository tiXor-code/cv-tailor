"""fetch_apify_linkedin: LinkedIn discovery via the Apify actor.

Two things make this source different from every other fetcher here, and both
are pinned below.

COST. cheap_scraper/linkedin-job-scraper is PAY PER RESULT on a FREE tier with
$5/month. Every gate that can prevent a network call must be checked BEFORE the
socket opens, and a misconfigured credential must never burn a paid slot.

TRUST. The actor's OUTPUT field names are not published in its input schema and
no run has succeeded, so the camelCase names asserted here are an ASSUMPTION.
The intended cheap probe turned out to be impossible: the live API rejects
anything under 150 results ("Field input.maxItems must be >= 150"), so the
smallest verification run is a real ~$0.11 one. Until that runs, the mapper
carries a canary: if billed rows arrive and map to nothing, that is a LOUD
failure naming the keys it saw, never a silent empty list.
"""
import json

import pytest

import cv_tailor.job_sources as js
from cv_tailor.job_sources import fetch_apify_linkedin


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Recorder:
    """Captures the Request without opening a socket."""

    def __init__(self, payload=None, error=None):
        self.payload = payload if payload is not None else []
        self.error = error
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        if self.error is not None:
            raise self.error
        return _FakeResp(self.payload)


class _StubBudget:
    # Default grant is above the actor's 150-result floor, because a grant
    # below it means "skip" rather than "run smaller" -- there is no such
    # thing as a cheaper run with this actor.
    def __init__(self, grant=500):
        self._grant = grant
        self.reserved = []
        self.settled = []

    def reserve(self, n):
        self.reserved.append(n)
        return min(n, self._grant)

    def settle(self, granted, actual):
        self.settled.append((granted, actual))


ROW = {
    "jobId": "3901234567",
    "jobTitle": "Applied AI Engineer",
    "companyName": "Acme Labs",
    "location": "Bucharest, Romania",
    "jobUrl": "https://www.linkedin.com/jobs/view/3901234567",
    "jobDescription": "<p>Build <b>LLM</b> pipelines.</p>",
    "workType": "remote",
}


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "apify_api_FAKE_TOKEN_VALUE")
    monkeypatch.setenv("APIFY_ENABLED", "1")
    monkeypatch.setattr(js, "_APIFY_STATE", {"runs": 0}, raising=False)


# --- cost gates: nothing may open a socket ---------------------------------

def test_no_ops_without_a_token_and_never_touches_the_budget(monkeypatch):
    """A misconfigured credential must not burn a pay-per-result slot. The
    token is checked BEFORE the budget and before any network."""
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)
    budget = _StubBudget()

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=budget)

    assert out == []
    assert rec.calls == [], "no network call without a token"
    assert budget.reserved == [], "the budget was not even consulted"


def test_is_off_by_default(monkeypatch):
    """Default OFF: a bare scan, a test, or an editor import can never bill."""
    monkeypatch.delenv("APIFY_ENABLED", raising=False)
    rec = _Recorder()
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    assert fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget()) == []
    assert rec.calls == []


def test_an_exhausted_budget_makes_no_network_call(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)
    budget = _StubBudget(grant=0)

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=budget, max_items=10)

    assert out == []
    assert rec.calls == [], "reserve() returned 0, so the socket must stay shut"


def test_a_missing_budget_skips_rather_than_running_unbudgeted(monkeypatch):
    """Deliberate inversion of the serp/jsearch default: this source costs money
    per ROW, so a caller that forgets to thread a budget gets nothing."""
    rec = _Recorder()
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    assert fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=None) == []
    assert rec.calls == []


# --- credential handling ---------------------------------------------------

def test_sends_the_token_as_a_header_never_in_the_url(monkeypatch):
    """A token in a query string lands in logs, proxies and redirect chains."""
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    req = rec.calls[0]
    assert "token=" not in req.full_url, f"token leaked into the URL: {req.full_url}"
    assert "FAKE_TOKEN_VALUE" not in req.full_url
    auth = req.get_header("Authorization") or ""
    assert auth.startswith("Bearer ") and "FAKE_TOKEN_VALUE" in auth


def test_the_token_never_reaches_a_warning_line(monkeypatch, capsys):
    """Errors are redacted: an HTTP failure whose message echoes the URL must
    not print the credential."""
    err = RuntimeError("boom apify_api_FAKE_TOKEN_VALUE leaked")
    rec = _Recorder(error=err)
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    printed = capsys.readouterr()
    assert "FAKE_TOKEN_VALUE" not in (printed.out + printed.err)


# --- request shape ---------------------------------------------------------

def test_posts_a_json_body_carrying_the_search(monkeypatch):
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["Applied AI Engineer", "AI Solutions Engineer"],
                         ["European Union"], published_at="r86400",
                         work_type=["remote"], budget=_StubBudget())

    req = rec.calls[0]
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode())
    assert body["keyword"] == ["Applied AI Engineer", "AI Solutions Engineer"]
    assert body["locations"] == ["European Union"]
    assert body["publishedAt"] == "r86400"
    assert body["workType"] == ["remote"]


def test_forces_unique_items_and_refuses_paid_enrichment(monkeypatch):
    """saveOnlyUniqueItems dedups BEFORE billing. enrichCompanyData costs extra
    for a field nothing in the funnel reads."""
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    body = json.loads(rec.calls[0].data.decode())
    assert body["saveOnlyUniqueItems"] is True
    assert body["enrichCompanyData"] is False


def test_caps_charged_results_in_both_the_url_and_the_body(monkeypatch):
    """maxItems in the body is the actor's own limit; maxItems in the URL is
    Apify's hard billing ceiling. Only the URL one is enforced if the actor
    misbehaves, so both must be present."""
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["AI Engineer"], ["Romania"], max_items=200,
                         budget=_StubBudget(grant=200))

    req = rec.calls[0]
    assert "maxItems=200" in req.full_url
    assert json.loads(req.data.decode())["maxItems"] == 200


def test_a_request_below_the_actor_floor_is_raised_to_it(monkeypatch):
    """MEASURED against the live API: the actor rejects anything smaller with
    "Field input.maxItems must be >= 150". A request for 12 is not a cheap
    run, it is a 400 -- so the floor is applied rather than the caller's
    smaller number."""
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["AI Engineer"], ["Romania"], max_items=12,
                         budget=_StubBudget(grant=500))

    body = json.loads(rec.calls[0].data.decode())
    assert body["maxItems"] == js._APIFY_ACTOR_MIN_ITEMS == 150
    assert f"maxItems={js._APIFY_ACTOR_MIN_ITEMS}" in rec.calls[0].full_url


def test_refuses_to_run_when_the_budget_cannot_cover_the_floor(monkeypatch):
    """There is no such thing as a smaller run with this actor. If the budget
    can only grant 25, sending a 25-item request buys a 400, and sending a
    150-item one spends allowance that was never reserved. Skip, and refund."""
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)
    budget = _StubBudget(grant=25)

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], max_items=150,
                               budget=budget)

    assert out == []
    assert rec.calls == [], "a partial grant must not become a doomed request"
    assert budget.settled == [(25, 0)], "the partial grant is refunded in full"


def test_sends_a_hard_dollar_ceiling_for_the_run(monkeypatch):
    """The actor is PAY_PER_EVENT ($0.0007/result + $0.005/GB start), so a
    result count is an indirect cost control. maxTotalChargeUsd is the direct
    one and Apify enforces it server-side."""
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget(grant=500))

    assert "maxTotalChargeUsd=" in rec.calls[0].full_url


def test_a_source_entry_cannot_raise_the_cap_without_bound(monkeypatch):
    """An absurd max_items in sources.yaml clamps to the module ceiling, in
    both the URL and the body."""
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    fetch_apify_linkedin(["AI Engineer"], ["Romania"], max_items=9999,
                         budget=_StubBudget(grant=9999))

    req = rec.calls[0]
    body = json.loads(req.data.decode())
    assert body["maxItems"] <= js._APIFY_MAX_ITEMS_CEILING
    assert f"maxItems={body['maxItems']}" in req.full_url


# --- budget settlement -----------------------------------------------------

def test_settles_the_unused_grant_back(monkeypatch):
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)
    budget = _StubBudget(grant=200)

    # Above the actor's 150 floor, so the request is made as asked.
    fetch_apify_linkedin(["AI Engineer"], ["Romania"], max_items=200, budget=budget)

    assert budget.reserved == [200]
    assert budget.settled == [(200, 1)], "granted 200, actually billed 1 row"


def test_settles_even_when_the_call_raises(monkeypatch):
    """The refund runs in a finally, or a transient failure permanently eats
    the whole grant."""
    rec = _Recorder(error=RuntimeError("network down"))
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)
    budget = _StubBudget(grant=200)

    # Above the 150 floor on purpose: with a smaller grant this returns before
    # any request is made, and would pass while testing nothing of the sort.
    fetch_apify_linkedin(["AI Engineer"], ["Romania"], max_items=200, budget=budget)

    assert rec.calls, "the request must actually have been attempted"
    assert budget.settled == [(200, 0)]


# --- mapping ---------------------------------------------------------------

def test_maps_the_actors_fields_onto_a_job_posting(monkeypatch):
    rec = _Recorder(payload=[ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    (post,) = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    assert post.source == "linkedin", "the marketplace, not the provider"
    assert post.org == "Acme Labs"
    assert post.title == "Applied AI Engineer"
    assert post.raw_id == "3901234567", "LinkedIn's own job id, never a URL"
    assert "LLM" in post.description
    assert "<b>" not in post.description, "HTML stripped like every other source"


def test_drops_a_row_with_no_job_id(monkeypatch):
    """raw_id is half the seen_jobs primary key and half the queue id basis, so
    a row without one would break dedup rather than merely be untidy."""
    rec = _Recorder(payload=[dict(ROW, jobId=None), ROW])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    assert [p.raw_id for p in out] == ["3901234567"]


def test_dedups_by_job_id_then_by_canonical_url(monkeypatch):
    rec = _Recorder(payload=[
        ROW,
        dict(ROW),                                             # same jobId
        dict(ROW, jobId="999", jobUrl=ROW["jobUrl"] + "?utm_source=x"),  # same url
    ])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    assert len(out) == 1


# --- the loud-failure guarantees -------------------------------------------

def test_warns_when_a_paid_run_returns_zero_rows(monkeypatch, capsys):
    """An empty output is not evidence of an empty market -- the repo's rule."""
    rec = _Recorder(payload=[])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    assert out == []
    # readouterr() DRAINS the buffer -- calling it twice in one expression
    # leaves the second read empty.
    captured = capsys.readouterr()
    printed = (captured.out + captured.err).lower()
    assert "empty result is not evidence" in printed or "0 rows" in printed


def test_canary_fires_when_billed_rows_map_to_nothing(monkeypatch, capsys):
    """The field names here are an ASSUMPTION until the probe verifies them. If
    the actor renames them, rows are billed and silently produce nothing --
    which must scream, and must name the keys actually seen so the fix is one
    read away."""
    rec = _Recorder(payload=[{"totally": "different", "keys": "here"}])
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    assert out == []
    captured = capsys.readouterr()
    printed = (captured.out + captured.err).lower()
    assert "totally" in printed, "the unmapped keys must be named in the warning"


def test_reports_the_http_status_on_a_credit_failure(monkeypatch, capsys):
    """402 means credit exhausted, and must not read as 'no jobs today'."""
    import urllib.error
    rec = _Recorder(error=urllib.error.HTTPError(
        "https://api.apify.com/x", 402, "Payment Required", {}, None))
    monkeypatch.setattr(js.urllib.request, "urlopen", rec)

    out = fetch_apify_linkedin(["AI Engineer"], ["Romania"], budget=_StubBudget())

    assert out == []
    captured = capsys.readouterr()
    assert "402" in (captured.out + captured.err)


# --- wiring ----------------------------------------------------------------

def test_fetch_all_dispatches_apify_linkedin_and_threads_the_budget(monkeypatch):
    """Every knob forwarded EXPLICITLY. The existing linkedin lambda's failure
    mode is that anything it does not name is silently ignored."""
    seen = {}

    def fake(keywords, locations, **kw):
        seen["keywords"] = keywords
        seen["locations"] = locations
        seen.update(kw)
        return []

    monkeypatch.setattr(js, "fetch_apify_linkedin", fake)
    budget = _StubBudget()

    js.fetch_all([{
        "kind": "apify_linkedin",
        "keywords": ["AI Engineer"],
        "locations": ["Romania"],
        "max_items": 9,
        "label": "romania-ai-llm-daily",
        "published_at": "r86400",
    }], apify_budget=budget)

    assert seen["keywords"] == ["AI Engineer"]
    assert seen["locations"] == ["Romania"]
    assert seen["max_items"] == 9
    assert seen["label"] == "romania-ai-llm-daily"
    assert seen["published_at"] == "r86400"
    assert seen["budget"] is budget, "the ONE shared budget must be threaded"


def test_fetch_all_skips_apify_when_no_budget_is_threaded(monkeypatch):
    """Deliberate inversion of the serp/jsearch default: unbudgeted means SKIP,
    because this source bills per row."""
    called = []
    monkeypatch.setattr(js, "fetch_apify_linkedin",
                        lambda k, l, **kw: called.append(kw) or [])

    js.fetch_all([{"kind": "apify_linkedin", "keywords": ["x"], "locations": ["y"]}])

    assert called == [] or called[0].get("budget") is None


def test_fetch_all_reads_no_credential_from_a_source_entry(monkeypatch):
    """SEC020: a live-looking value in a TRACKED file must never be forwarded.
    The endpoint, header and key are env-only."""
    seen = {}
    monkeypatch.setattr(js, "fetch_apify_linkedin",
                        lambda k, l, **kw: (seen.update(kw), [])[1])

    js.fetch_all([{
        "kind": "apify_linkedin", "keywords": ["x"], "locations": ["y"],
        "token": "apify_api_SHOULD_BE_IGNORED",
        "api_key": "also_ignored",
        "apify_token": "still_ignored",
    }], apify_budget=_StubBudget())

    # repr, not json.dumps: the forwarded kwargs legitimately contain the
    # budget object, and a leaked credential must be caught wherever it hides.
    assert "SHOULD_BE_IGNORED" not in repr(seen)
    assert "also_ignored" not in repr(seen)
    assert "still_ignored" not in repr(seen)
    assert not any(k in seen for k in ("token", "api_key", "apify_token"))
