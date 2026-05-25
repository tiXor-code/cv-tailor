from datetime import date
from cv_tailor.slug import job_slug, kebab

def test_kebab_basic():
    assert kebab("Hello World") == "hello-world"

def test_kebab_strips_punctuation():
    assert kebab("Senior AI/ML Engineer!") == "senior-ai-ml-engineer"

def test_kebab_collapses_runs():
    assert kebab("  too   many   spaces  ") == "too-many-spaces"

def test_kebab_handles_unicode_accents():
    assert kebab("Ministeru' Creativ") == "ministeru-creativ"

def test_job_slug_format():
    assert job_slug("Instantly", "AI Automation Engineer", on=date(2026, 5, 26)) \
        == "2026-05-26-instantly-ai-automation-engineer"

def test_job_slug_uses_today_by_default():
    s = job_slug("Acme", "Engineer")
    assert s[10] == "-"
    assert s.endswith("-acme-engineer")
