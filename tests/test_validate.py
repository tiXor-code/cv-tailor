import json
from cv_tailor.profile import load_profile
from cv_tailor.validate import validate

def test_validate_accepts_valid_fields(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    assert validate(profile, fields) == []

def test_validate_flags_unknown_experience_id(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_invalid_experience.json").read_text())
    errors = validate(profile, fields)
    assert any("nonexistent" in e for e in errors)

def test_validate_flags_bullet_index_out_of_range(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fields["experience_bullets"]["job1"] = [99]
    errors = validate(profile, fields)
    assert any("bullet index" in e.lower() for e in errors)

def test_validate_flags_unknown_project_id(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fields["project_ids"] = ["ghost_project"]
    errors = validate(profile, fields)
    assert any("ghost_project" in e for e in errors)

def test_validate_flags_unknown_skill(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fields["skills_emphasis"] = ["Cobol"]
    errors = validate(profile, fields)
    assert any("Cobol" in e for e in errors)

def test_validate_flags_unknown_summary_id(fixtures_dir):
    profile = load_profile(fixtures_dir / "profile_minimal.yaml")
    fields = json.loads((fixtures_dir / "fields_valid.json").read_text())
    fields["chosen_summary_id"] = "nope"
    errors = validate(profile, fields)
    assert any("nope" in e for e in errors)
