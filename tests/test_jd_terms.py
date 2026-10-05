"""cv_tailor.jd_terms -- JD term extraction and profile-grounded coverage.
All data fictional (tests/fixtures/ats/)."""
from pathlib import Path

import yaml

from cv_tailor.jd_terms import coverage, extract_terms, profile_text, term_present

FIX = Path(__file__).parent / "fixtures" / "ats"
JD = (FIX / "jd_sample.txt").read_text()
PROFILE = yaml.safe_load((FIX / "profile_ats.yaml").read_text())


def _terms(**kw):
    return [t["term"] for t in extract_terms(JD, **kw)]


def test_acronyms_kept_and_ranked():
    terms = _terms()
    assert "RAG" in terms and "LLM" in terms
    assert terms.index("RAG") < 5  # repeated four times


def test_stopwords_and_boilerplate_removed():
    terms = {t.lower() for t in _terms(top_n=100)}
    for noise in ("the", "and", "you", "competitive", "salary", "benefits", "team", "experience"):
        assert noise not in terms


def test_repeated_phrases_extracted_single_mentions_not():
    terms = _terms(top_n=100)
    assert "data pipelines" in terms          # said twice
    assert "evaluation harness" not in terms  # said once


def test_exclude_drops_company_name():
    terms = {t.lower() for t in _terms(top_n=100, exclude=["Acme Robotics"])}
    assert "acme" not in terms and "robotics" not in terms


def test_phrases_do_not_cross_punctuation():
    terms = [t["term"] for t in extract_terms("Python, SQL. Python, SQL. Python, SQL.", top_n=10)]
    assert "python sql" not in terms


def test_term_present_uses_equivalents_and_plurals():
    assert term_present("RAG", "built a retrieval-augmented generation service")
    assert term_present("LLM", "worked with large language models")
    assert term_present("agents", "an agent that triages")
    assert not term_present("RAG", "storage and drag-and-drop")  # whole words only


def test_coverage_splits_supported_from_unsupported():
    cv = "Jane Example. Built data pipelines in Python and PostgreSQL. Document search service."
    cov = coverage(JD, cv, PROFILE, exclude=["Acme Robotics"])
    assert "RAG" in cov["missing_supported"]      # profile says retrieval-augmented generation
    assert "LLM" in cov["missing_supported"]      # profile says large language models
    assert "Kubernetes" not in cov["missing_supported"]
    assert "kubernetes" in [t.lower() for t in cov["missing_unsupported"]]
    assert "terraform" in [t.lower() for t in cov["missing_unsupported"]]
    assert "python" in [t.lower() for t in cov["matched"]]
    assert 0 < cov["match_pct"] < 100


def test_never_supported_without_profile_text():
    cov = coverage(JD, "", {"contact": {"name": "Nobody"}})
    assert cov["missing_supported"] == []
    assert cov["match_pct"] == 0


def test_profile_text_flattens_nested_values():
    text = profile_text(PROFILE)
    assert "retrieval-augmented generation" in text
    assert "Example University" in text


class _FakeLLM:
    def __init__(self, rows):
        import json
        from types import SimpleNamespace as NS
        payload = json.dumps({"terms": rows})
        self.chat = NS(completions=NS(create=lambda **kw: NS(choices=[NS(message=NS(content=payload))])))


def test_judge_accepts_only_verbatim_profile_evidence():
    profile = {"summary": "Builds agents with Claude Code every day for client automation."}
    rows = [
        {"term": "ai development tools", "supported": True, "evidence": "Claude Code every day"},
        {"term": "kubernetes", "supported": True, "evidence": "ran Kubernetes clusters at scale"},  # not in profile
        {"term": "healthcare", "supported": False, "evidence": ""},
        {"term": "evals", "supported": True, "evidence": "agents"},  # too short to count
    ]
    from cv_tailor.jd_terms import judge_support, apply_judgement
    got = judge_support(["ai development tools", "kubernetes", "healthcare", "evals"], profile,
                        client=_FakeLLM(rows))
    assert got == {"ai development tools": "Claude Code every day"}
    cov = {"missing_supported": ["rag"], "missing_unsupported": ["ai development tools", "kubernetes"]}
    moved = apply_judgement(cov, got)
    assert moved["missing_supported"] == ["rag", "ai development tools"]
    assert moved["missing_unsupported"] == ["kubernetes"]


def test_judge_failure_keeps_the_literal_answer():
    from cv_tailor.jd_terms import judge_support

    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("down")
    assert judge_support(["x y"], {"a": "b"}, client=Boom()) == {}
    assert judge_support(["x y"], {"a": "b"}, client=None) == {}
