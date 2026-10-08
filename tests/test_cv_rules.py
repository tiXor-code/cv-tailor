"""cv_tailor.cv_rules: docs/cv-rules.md as code. Fixture text is fictional."""
from cv_tailor.cv_rules import (
    apply_cv_rules,
    banned_hits,
    has_result_number,
    headline_problems,
    lint_cv_text,
    summary_problems,
)
from cv_tailor.render import emphasize
from cv_tailor.tailor_llm import SYSTEM_PROMPT

PROFILE = {
    "contact": {"headline": "AI Automation Specialist | Agentic workflows"},
    "summary_pool": [
        {"id": "main", "text": "AI automation builder who ships with Claude Code. "
                               "One site is cited for 12 of 20 target searches."},
        {"id": "other", "text": "Builder of internal tools."},
    ],
    "experiences": [{"id": "acme", "role": "Lead", "company": "Acme", "dates": "Mar 2024 – Present",
                     "bullets": ["Cut report time by 40% for 3 clients."]}],
    "projects": [],
    "education": [{"degree": "BSc Example", "year": 2020}],
}

GOOD_SUMMARY = ("AI automation builder who ships with Claude Code. "
                "Cut report time by 40% for 3 clients. Works remotely from Bucharest.")


def test_banned_hits_catch_filler_and_ai_vocabulary():
    hits = banned_hits("Leveraged robust pipelines and helped the team, significantly.")
    assert hits == ["leveraged", "robust", "helped", "significantly"]


def test_banned_hits_skip_hyphenated_and_longer_words():
    # "AI-assisted" is a method, not the helper verb; "Assistant" is a title.
    assert banned_hits("AI-assisted development as an Assistant Content Producer") == []


def test_good_summary_passes():
    assert summary_problems(GOOD_SUMMARY, PROFILE) == []


def test_summary_problems_name_each_rule():
    bad = ("With a 2020 university dissertation behind me, I am a passionate builder. "
           "My agents are robust. They run daily. They are 99% reliable — always.")
    problems = " | ".join(summary_problems(bad, PROFILE))
    assert "sentences" in problems
    assert "pronouns" in problems
    assert "opens with education" in problems
    assert "banned words" in problems and "passionate" in problems and "robust" in problems
    assert "em dash" in problems
    assert "numbers not in profile.yaml: 99" in problems


def test_summary_word_limit():
    long = "Builder " * 71
    assert any("71 words" in p for p in summary_problems(long, PROFILE))


def test_numbers_inside_names_are_not_checked():
    # n8n, C2 and 9.3K are names or unit-glued figures, not loose numbers.
    text = "Ships n8n automations at C2 English level across 9.3K records for 3 clients."
    assert summary_problems(text, PROFILE) == []


def test_headline_problems():
    assert headline_problems("AI Solutions Consultant | RAG, n8n", PROFILE) == []
    assert headline_problems("", PROFILE) == ["empty"]
    assert any("characters" in p for p in headline_problems("A" * 101, PROFILE))
    assert any("banned" in p for p in headline_problems("Results-driven AI expert", PROFILE))


def test_apply_cv_rules_keeps_compliant_text():
    fields = {"headline": "AI Solutions Consultant | RAG", "summary_rewrite": GOOD_SUMMARY,
              "chosen_summary_id": "other"}
    assert apply_cv_rules(PROFILE, fields, summary_id="main") == []
    assert fields["summary_rewrite"] == GOOD_SUMMARY
    assert fields["headline"] == "AI Solutions Consultant | RAG"


def test_apply_cv_rules_replaces_breaking_summary_with_track_summary():
    fields = {"summary_rewrite": "I am a passionate builder with 99 agents.",
              "chosen_summary_id": "other"}
    notes = apply_cv_rules(PROFILE, fields, summary_id="main")
    assert fields["summary_rewrite"].startswith("AI automation builder who ships with Claude Code.")
    assert len(notes) == 1 and "summary replaced" in notes[0] and "pronouns" in notes[0]
    # A missing headline falls back silently to the profile's.
    assert fields["headline"] == PROFILE["contact"]["headline"]


def test_apply_cv_rules_falls_back_to_chosen_summary_without_track():
    fields = {"summary_rewrite": "", "chosen_summary_id": "other"}
    apply_cv_rules(PROFILE, fields)
    assert fields["summary_rewrite"] == "Builder of internal tools."


def test_apply_cv_rules_replaces_breaking_headline():
    fields = {"headline": "Visionary — seamless AI leader", "summary_rewrite": GOOD_SUMMARY}
    notes = apply_cv_rules(PROFILE, fields, summary_id="main")
    assert fields["headline"] == PROFILE["contact"]["headline"]
    assert notes and notes[0].startswith("headline replaced")


def test_has_result_number_ignores_years():
    assert has_result_number("Cut timelines by 30% with Agile sprints.")
    assert has_result_number("Cited for 51 of 55 target searches.")
    assert not has_result_number("Experiment run in Q1 2026 with the team.")
    assert not has_result_number("Ships n8n automations.")


def test_lint_cv_text_measures_bullets_and_flags_breaches():
    text = "\n".join([
        "Experience",
        "  • Cut report time by 40% for 3 clients through one shared",
        "    dashboard.",
        "  • Helped the team with SGEO — weekly.",
        "  • Runs client delivery across websites and content.",
        "Skills",
    ])
    report = lint_cv_text(text)
    assert report["bullets"] == 3
    assert report["bullets_with_result"] == 1
    assert report["result_ratio"] == 0.33
    assert report["banned"] == ["helped"]
    assert report["em_dashes"] == 1
    assert report["internal_names"] == ["SGEO"]
    assert any("1 of 3 bullets" in w for w in report["warnings"])


def test_lint_cv_text_clean_cv_has_no_warnings():
    text = "  • Cut report time by 40% for 3 clients.\n  • Launched 2,361 pages.\n"
    assert lint_cv_text(text)["warnings"] == []


def test_emphasize_bolds_first_occurrence_and_escapes():
    out = str(emphasize("Cut <b>time</b> by 30% and 30% again", ["30%", "absent phrase"]))
    assert out == "Cut &lt;b&gt;time&lt;/b&gt; by <strong>30%</strong> and 30% again"


def test_emphasize_with_no_phrases_only_escapes():
    assert str(emphasize("A & B", None)) == "A &amp; B"


def test_prompt_carries_the_rules():
    low = " ".join(SYSTEM_PROMPT.lower().split())
    assert '"headline"' in low and '"skills_groups"' in low
    assert "never open with education or the dissertation" in low
    assert "no em dashes" in low
    assert "vibe coding" in low
    assert "at most 3 sentences and 70 words" in low
    assert "—" not in SYSTEM_PROMPT
    # The old rule that made every summary open with the 2022 dissertation is gone.
    assert "leads with his strongest ai evidence (the 2022 ai dissertation" not in low


def test_docx_bullets_bold_only_the_emphasis_phrase():
    from docx import Document

    from cv_tailor.variants import _add_emphasized

    para = Document().add_paragraph()
    _add_emphasized(para, "Raised tested volume by 63% with test automation.", ["63%", "missing"])
    assert [(r.text, bool(r.bold)) for r in para.runs] == [
        ("Raised tested volume by ", False), ("63%", True), (" with test automation.", False)]


def test_apply_cv_rules_drops_projects_already_shown_under_a_role():
    profile = dict(PROFILE, projects=[{"id": "dup", "covered_by": "acme"}, {"id": "own"}])
    fields = {"summary_rewrite": GOOD_SUMMARY, "experience_ids_ordered": ["acme"],
              "project_ids": ["dup", "own"]}
    notes = apply_cv_rules(profile, fields, summary_id="main")
    assert fields["project_ids"] == ["own"]
    assert notes == ["projects dropped (demo, or already shown under a role): dup"]
    # When the covering role is not on the CV, the project stays.
    fields = {"summary_rewrite": GOOD_SUMMARY, "experience_ids_ordered": [], "project_ids": ["dup"]}
    apply_cv_rules(profile, fields, summary_id="main")
    assert fields["project_ids"] == ["dup"]


def test_apply_cv_rules_drops_demo_projects():
    profile = dict(PROFILE, projects=[{"id": "demo", "status": "demo"}, {"id": "real"}])
    fields = {"summary_rewrite": GOOD_SUMMARY, "project_ids": ["demo", "real"]}
    apply_cv_rules(profile, fields, summary_id="main")
    assert fields["project_ids"] == ["real"]
