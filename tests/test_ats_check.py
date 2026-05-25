from cv_tailor.ats_check import run_checks

GOOD_TEXT = """\
Test User
Nowhere - test@example.com - +1 555 555 5555 - linkedin.com/in/test
Summary
A tailored summary.

Experience
TestCo - Engineer
Jan 2024 - Present
- Did stuff.

Skills
Languages: Python

Education
BSc Testing - Test U - 2020
"""

def test_run_checks_passes_for_good_text():
    profile = {"contact": {"name": "Test User", "email": "test@example.com",
                            "phone": "+1 555 555 5555"}}
    fields = {"experience_ids_ordered": ["job1"], "project_ids": []}
    warnings = run_checks(GOOD_TEXT, profile, fields,
                          experiences_by_id={"job1": {"company": "TestCo"}},
                          projects_by_id={})
    assert warnings == []

def test_run_checks_flags_missing_section():
    text = GOOD_TEXT.replace("Skills", "Talents")
    warnings = run_checks(text, {"contact": {"name": "x", "email": "test@example.com",
                                              "phone": "+1 555 555 5555"}},
                          {"experience_ids_ordered": [], "project_ids": []},
                          experiences_by_id={}, projects_by_id={})
    assert any("Skills" in w for w in warnings)

def test_run_checks_flags_spaced_letter_mangling():
    text = "T e s t   U s e r\nSummary\nExperience\nSkills\nEducation\n"
    warnings = run_checks(text, {"contact": {"name": "Test User", "email": "x@y",
                                              "phone": "+1"}},
                          {"experience_ids_ordered": [], "project_ids": []},
                          experiences_by_id={}, projects_by_id={})
    assert any("spaced" in w.lower() or "mangl" in w.lower() for w in warnings)

def test_run_checks_flags_missing_contact_in_first_lines():
    text = "Title\n\n\n\n\n\n\n\nSummary\nExperience\nSkills\nEducation\ntest@example.com"
    warnings = run_checks(text, {"contact": {"name": "x", "email": "test@example.com",
                                              "phone": "+1 555 555 5555"}},
                          {"experience_ids_ordered": [], "project_ids": []},
                          experiences_by_id={}, projects_by_id={})
    assert any("email" in w.lower() for w in warnings)
