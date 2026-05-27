import json
from io import BytesIO
from unittest.mock import patch, MagicMock

from cv_tailor.job_sources import (
    _strip_html,
    fetch_ashby_org,
    fetch_all,
    JobPosting,
)


def test_strip_html_basic():
    assert _strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_strip_html_entities_and_whitespace():
    out = _strip_html("<p>Foo&nbsp;&amp; bar  &lt;baz&gt;</p>\n\n<p>qux</p>")
    assert out == "Foo & bar <baz> qux"


def test_strip_html_handles_empty():
    assert _strip_html("") == ""
    assert _strip_html(None) == ""


def _mock_urlopen_payload(payload: dict):
    """Return a context-manager mock whose .read() yields json bytes."""
    cm = MagicMock()
    cm.__enter__.return_value = BytesIO(json.dumps(payload).encode("utf-8"))
    cm.__exit__.return_value = False
    return cm


def test_fetch_ashby_returns_postings():
    payload = {
        "jobs": [
            {
                "id": "abc123",
                "title": "Software Engineer - AI Systems",
                "location": "Europe (Remote)",
                "employmentType": "Full time",
                "descriptionHtml": "<p>Build <b>cool</b> AI things.</p>",
                "jobUrl": "https://jobs.ashbyhq.com/xbow/abc123",
                "secondaryLocations": [{"location": "Berlin"}, {"location": "Lisbon"}],
                "team": "Engineering",
            }
        ]
    }
    with patch("cv_tailor.job_sources.urllib.request.urlopen",
               return_value=_mock_urlopen_payload(payload)):
        out = fetch_ashby_org("xbowcareers", display_name="XBow")

    assert len(out) == 1
    p = out[0]
    assert isinstance(p, JobPosting)
    assert p.source == "ashby"
    assert p.org == "XBow"
    assert p.title == "Software Engineer - AI Systems"
    assert "Europe (Remote)" in p.location
    assert "Berlin" in p.location
    assert "Lisbon" in p.location
    assert p.url == "https://jobs.ashbyhq.com/xbow/abc123"
    assert p.description == "Build cool AI things."
    assert p.raw_id == "abc123"


def test_fetch_ashby_handles_missing_fields():
    payload = {"jobs": [{"id": "x"}]}
    with patch("cv_tailor.job_sources.urllib.request.urlopen",
               return_value=_mock_urlopen_payload(payload)):
        out = fetch_ashby_org("someorg")
    assert len(out) == 1
    assert out[0].org == "someorg"
    assert out[0].title == ""
    assert out[0].description == ""


def test_fetch_all_swallows_errors_per_source():
    payload_ok = {"jobs": [{"id": "1", "title": "AI Eng", "descriptionHtml": "<p>x</p>",
                            "jobUrl": "u", "location": "Remote"}]}

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=15):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return _mock_urlopen_payload(payload_ok)

    with patch("cv_tailor.job_sources.urllib.request.urlopen", side_effect=fake_urlopen):
        out = fetch_all([
            {"kind": "ashby", "slug": "broken", "name": "Broken"},
            {"kind": "ashby", "slug": "working", "name": "Working"},
        ])

    assert len(out) == 1
    assert out[0].org == "Working"
