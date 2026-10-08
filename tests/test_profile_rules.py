"""The real profile.yaml obeys docs/cv-rules.md. The generator copies bullets
verbatim and falls back to these summaries, so a rule broken here would
reach every CV."""
from pathlib import Path

import pytest

from cv_tailor.cv_rules import EM_DASH, banned_hits, headline_problems, internal_names, summary_problems
from cv_tailor.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = load_profile(ROOT / "profile.yaml")


def _texts(item: dict) -> list[str]:
    keys = ("role", "company", "description", "name", "tagline")
    return [str(item[k]) for k in keys if item.get(k)] + [str(b) for b in item.get("bullets", []) or []]


@pytest.mark.parametrize("summary", PROFILE["summary_pool"], ids=lambda s: s["id"])
def test_every_profile_summary_passes(summary):
    assert summary_problems(summary["text"], PROFILE) == []


def test_profile_headline_passes():
    assert headline_problems(PROFILE["contact"]["headline"], PROFILE) == []


@pytest.mark.parametrize("kind", ["experiences", "projects"])
def test_no_banned_words_dashes_or_internal_names(kind):
    breaches = []
    for item in PROFILE[kind]:
        for text in _texts(item):
            if banned_hits(text) or internal_names(text) or EM_DASH in text:
                breaches.append(f"{item['id']}: {text[:80]}")
    assert breaches == []


@pytest.mark.parametrize("kind", ["experiences", "projects"])
def test_every_emphasis_phrase_is_in_a_bullet(kind):
    # Bold can only highlight words that are already there (R8).
    missing = [(item["id"], phrase) for item in PROFILE[kind]
               for phrase in item.get("emphasis", []) or []
               if not any(phrase in str(b) for b in item.get("bullets", []) or [])]
    assert missing == []


def test_agency_role_leads_with_results():
    agency = next(e for e in PROFILE["experiences"] if e["id"] == "ministeru")
    assert agency["description"]
    assert "51 of 55 target searches" in agency["bullets"][0]
