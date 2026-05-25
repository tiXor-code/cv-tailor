import pytest
from pathlib import Path
from cv_tailor.profile import load_profile, detect_gaps, ProfileGapError

def test_load_profile_parses_minimal_fixture(fixtures_dir):
    p = load_profile(fixtures_dir / "profile_minimal.yaml")
    assert p["contact"]["name"] == "Test User"
    assert p["experiences"][0]["id"] == "job1"

def test_detect_gaps_returns_empty_for_clean_profile(fixtures_dir):
    p = load_profile(fixtures_dir / "profile_minimal.yaml")
    assert detect_gaps(p) == []

def test_detect_gaps_finds_fill_in_markers():
    p = {"experiences": [{"id": "x", "dates": "<fill in start> – Present"}]}
    gaps = detect_gaps(p)
    assert any("experiences[0].dates" in g for g in gaps)

def test_load_profile_raises_when_gaps_present_and_strict(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("contact:\n  name: <fill in name>\n")
    with pytest.raises(ProfileGapError):
        load_profile(p, strict=True)
