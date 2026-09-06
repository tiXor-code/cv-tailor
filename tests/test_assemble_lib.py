"""cv_tailor.assemble -- the assemble_package library core.

Extracted from scripts/assemble.py so both the manual CLI and the
apply_approved.py orchestrator call the same pipeline. tailor, cover_letter,
and render_pdf are monkeypatched (no LLM/Azure/WeasyPrint calls); render_html
runs for real against the repo's own templates so the on-disk artifacts are
genuine.
"""
import json
from pathlib import Path

import pytest

from cv_tailor import assemble as assemble_mod
from cv_tailor.assemble import AssembleError, assemble_package

ROOT = Path(__file__).resolve().parent.parent

FIELDS = {
    "job_meta": {
        "company": "placeholder", "role": "placeholder",
        "location": "Remote", "jd_url": None, "seniority_signal": "mid",
    },
    "chosen_summary_id": "default",
    "summary_rewrite": "Tailored summary.",
    "experience_ids_ordered": ["job1"],
    "experience_bullets": {"job1": [0]},
    "project_ids": [],
    "skills_emphasis": ["Python"],
    "jd_keywords_matched": ["python"],
    "gaps_honest": [],
    "one_line_pitch": "I am a fit.",
}

CLEAN_LETTER = " ".join(["shipped"] * 140)


def _fake_tailor(profile, jd_text, *, client=None, deployment=None):
    return json.loads(json.dumps(FIELDS))  # fresh deep copy every call


def _fake_cover_letter(profile, jd_text, fields, *, client=None, deployment=None):
    return CLEAN_LETTER


def _fake_render_pdf(html, css_path, out_path):
    Path(out_path).write_bytes(b"%PDF-1.4 fake pdf bytes\n")
    return Path(out_path)


@pytest.fixture(autouse=True)
def _patch_llm_and_pdf(monkeypatch):
    """Per Step 1 of the brief: mock tailor/cover_letter/render_pdf everywhere."""
    monkeypatch.setattr(assemble_mod, "tailor", _fake_tailor)
    monkeypatch.setattr(assemble_mod, "cover_letter", _fake_cover_letter)
    monkeypatch.setattr(assemble_mod, "render_pdf", _fake_render_pdf)
    monkeypatch.setenv("CV_TAILOR_PROFILE", str(ROOT / "tests" / "fixtures" / "profile_minimal.yaml"))
    monkeypatch.setenv("CV_TAILOR_TEMPLATES", str(ROOT / "templates"))


def _entry(**overrides):
    base = {
        "id": "abc123",
        "title": "Engineer",
        "company": "TestCo",
        "location": "Remote",
        "url": "https://acme.example/jobs/1",
        "source": "lever",  # not "ashby" -- no fallback re-fetch to worry about
        "apply_method": "email",
        "apply_target": "jobs@testco.example",
    }
    base.update(overrides)
    return base


def _seed_description(queue_dir, scan_date, job_id, description):
    day_dir = queue_dir / scan_date
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "descriptions.json").write_text(json.dumps({job_id: description}))


def test_assemble_package_writes_files_and_returns_meta(tmp_path):
    entry = _entry()
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")

    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    pkg_dir = Path(result["package_dir"])
    assert pkg_dir.exists()
    assert pkg_dir == tmp_path / "2026-07-10" / "packages" / result["slug"]
    assert Path(result["cv_path"]) == pkg_dir / "cv.pdf"
    assert Path(result["cover_letter_path"]) == pkg_dir / "cover_letter.md"
    assert (pkg_dir / "cv.html").exists()
    assert (pkg_dir / "cv.pdf").read_bytes().startswith(b"%PDF")
    assert (pkg_dir / "cover_letter.md").read_text().strip() == CLEAN_LETTER
    assert (pkg_dir / "meta.json").exists()
    meta_on_disk = json.loads((pkg_dir / "meta.json").read_text())
    assert meta_on_disk["job_id"] == "abc123"

    assert result["job_id"] == "abc123"
    assert result["company"] == "TestCo"
    assert result["role"] == "Engineer"
    assert result["cover_letter_warnings"] == []
    assert result["cover_letter_words"] == 140


def test_assemble_package_no_write_back_to_queue(tmp_path):
    """assemble_package must NOT touch jobs.json -- the orchestrator owns that."""
    entry = _entry()
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")
    day_dir = tmp_path / "2026-07-10"
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "jobs.json").write_text(json.dumps([entry]))

    assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    entries = json.loads((day_dir / "jobs.json").read_text())
    assert entries[0].get("package_dir") is None
    assert entries[0].get("status", "pending") != "assembled"


def test_assemble_package_missing_jd_raises_assemble_error(tmp_path):
    entry = _entry(id="no-jd-job")
    # no descriptions.json sidecar written at all, source != ashby, no inline description
    with pytest.raises(AssembleError):
        assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")


def test_assemble_package_uses_inline_description_when_no_sidecar(tmp_path):
    entry = _entry(id="inline-job", description="We need a Python engineer, inline JD.")
    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")
    assert result["jd_source"] == "inline"


def test_assemble_package_validation_failure_raises_assemble_error(tmp_path, monkeypatch):
    """A tailor() result referencing an experience id outside profile.experiences
    trips the honesty guard -- assemble_package must surface this as
    AssembleError, not a raw exception, and must not have written package
    files. (Unknown skills_emphasis is handled separately -- see
    test_assemble_package_drops_unknown_skills_emphasis -- dropping a claimed
    skill can't fabricate anything, so it must not dead-end the assembly the
    way an invented experience/project/summary id would.)"""
    bad_fields = json.loads(json.dumps(FIELDS))
    bad_fields["experience_ids_ordered"] = ["no-such-experience"]
    monkeypatch.setattr(assemble_mod, "tailor", lambda *a, **kw: bad_fields)

    entry = _entry(id="bad-experience-job")
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")

    with pytest.raises(AssembleError):
        assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    assert not (tmp_path / "2026-07-10" / "packages").exists()


def test_assemble_package_drops_unknown_skills_emphasis(tmp_path, monkeypatch):
    """An LLM-invented skill not present anywhere in profile.skills (real e2e
    case: 'automation') must NOT dead-end the assembly -- dropping a claimed
    emphasis is honest (it can't fabricate anything), unlike an invented
    experience/project/summary id which references content that would then
    render as fact. The dropped skill is recorded in meta['skills_dropped']
    and must not reach the fields handed to rendering/cover-letter."""
    bad_fields = json.loads(json.dumps(FIELDS))
    bad_fields["skills_emphasis"] = ["Python", "automation"]
    monkeypatch.setattr(assemble_mod, "tailor", lambda *a, **kw: bad_fields)

    seen_fields = {}

    def _capturing_cover_letter(profile, jd_text, fields, *, client=None, deployment=None):
        seen_fields["skills_emphasis"] = list(fields.get("skills_emphasis", []))
        return CLEAN_LETTER

    monkeypatch.setattr(assemble_mod, "cover_letter", _capturing_cover_letter)

    entry = _entry(id="bogus-skill-job")
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")

    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    assert result["skills_dropped"] == ["automation"]
    assert seen_fields["skills_emphasis"] == ["Python"]
    assert "automation" not in seen_fields["skills_emphasis"]

    meta_on_disk = json.loads((Path(result["package_dir"]) / "meta.json").read_text())
    assert meta_on_disk["skills_dropped"] == ["automation"]


def test_assemble_package_skills_dropped_empty_when_none_dropped(tmp_path):
    """No unknown skills -> meta['skills_dropped'] is present but empty."""
    entry = _entry(id="clean-skill-job")
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")

    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    assert result["skills_dropped"] == []


# --- Task B2: track-aware scoring + assembly ---------------------------------

TRACKS_PROFILE = str(ROOT / "tests" / "fixtures" / "profile_tracks.yaml")


def _fake_tailor_capturing(captured, chosen_summary_id):
    """Records the exact profile dict handed to tailor() (so a test can
    inspect what summary_pool the LLM prompt would have been built from) and
    returns FIELDS with chosen_summary_id overridden to the track's id."""
    def _tailor(profile, jd_text, *, client=None, deployment=None):
        captured["profile"] = profile
        fields = json.loads(json.dumps(FIELDS))
        fields["chosen_summary_id"] = chosen_summary_id
        return fields
    return _tailor


def _capturing_cover_letter_factory(seen_fields):
    def _cover_letter(profile, jd_text, fields, *, client=None, deployment=None):
        seen_fields["skills_groups"] = fields.get("skills_groups")
        return CLEAN_LETTER
    return _cover_letter


def test_assemble_package_content_track_selects_summary_and_groups(tmp_path, monkeypatch):
    """track='content' constrains the tailor prompt's summary_pool to only the
    content track's summary_id, and restricts fields.skills_groups (the
    template's selectable-groups hook) to the content track's list."""
    monkeypatch.setenv("CV_TAILOR_PROFILE", TRACKS_PROFILE)
    captured = {}
    monkeypatch.setattr(assemble_mod, "tailor", _fake_tailor_capturing(captured, "content_summary"))
    seen_fields = {}
    monkeypatch.setattr(assemble_mod, "cover_letter", _capturing_cover_letter_factory(seen_fields))

    entry = _entry(id="content-job", track="content")
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a freelance video editor.")

    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    tailor_pool_ids = {s["id"] for s in captured["profile"]["summary_pool"]}
    assert tailor_pool_ids == {"content_summary"}

    assert seen_fields["skills_groups"] == ["content", "tools"]
    assert result["track"] == "content"

    meta_on_disk = json.loads((Path(result["package_dir"]) / "meta.json").read_text())
    assert meta_on_disk["track"] == "content"


def test_assemble_package_legacy_entry_no_track_key_defaults_to_ai(tmp_path, monkeypatch):
    """An entry with no 'track' key at all (a pre-B1 queue row) resolves to the
    ai track when profile.yaml has a tracks: config -- real track-aware
    behavior, not just 'no restriction'."""
    monkeypatch.setenv("CV_TAILOR_PROFILE", TRACKS_PROFILE)
    captured = {}
    monkeypatch.setattr(assemble_mod, "tailor", _fake_tailor_capturing(captured, "ai_summary"))
    seen_fields = {}
    monkeypatch.setattr(assemble_mod, "cover_letter", _capturing_cover_letter_factory(seen_fields))

    entry = _entry(id="legacy-job")
    entry.pop("track", None)
    assert "track" not in entry
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")

    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    tailor_pool_ids = {s["id"] for s in captured["profile"]["summary_pool"]}
    assert tailor_pool_ids == {"ai_summary"}
    assert seen_fields["skills_groups"] == ["languages", "tools"]
    assert result["track"] == "ai"


def test_assemble_package_unknown_track_falls_back_to_ai(tmp_path, monkeypatch):
    """A track id that isn't in profile['tracks'] (typo, or a newer schema this
    profile predates) falls back to the ai track's config, not a crash."""
    monkeypatch.setenv("CV_TAILOR_PROFILE", TRACKS_PROFILE)
    captured = {}
    monkeypatch.setattr(assemble_mod, "tailor", _fake_tailor_capturing(captured, "ai_summary"))

    entry = _entry(id="bogus-track-job", track="design")
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")

    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    tailor_pool_ids = {s["id"] for s in captured["profile"]["summary_pool"]}
    assert tailor_pool_ids == {"ai_summary"}
    assert result["track"] == "ai"


def test_assemble_package_missing_tracks_config_no_restriction(tmp_path):
    """profile.yaml with no tracks: block at all (profile_minimal.yaml, used by
    every other test in this file) must never crash, and must not inject
    fields.skills_groups -- every profile.skills group renders, unrestricted,
    exactly as before this track-aware assembly existed."""
    entry = _entry(id="no-tracks-config-job")
    assert "track" not in entry
    _seed_description(tmp_path, "2026-07-10", entry["id"], "We need a Python engineer.")

    result = assemble_package(entry, "2026-07-10", queue_dir=tmp_path, client="unused-sentinel")

    assert result["track"] == "ai"
    html = (Path(result["package_dir"]) / "cv.html").read_text()
    assert "Languages:" in html
