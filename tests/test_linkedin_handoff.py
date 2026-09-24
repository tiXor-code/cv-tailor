"""Supervised LinkedIn Easy Apply -- Teodor, 2026-09-24: option B, 10 a day.

Scout never touches his LinkedIn account. For a LinkedIn job that clears the
funnel but has no form Scout can fill (~92% of LinkedIn rows in the first
measured run), Scout sends HIM the link, the tailored CV and cover letter, and
an answer sheet, and he clicks Easy Apply himself. Easy Apply's questions are
only visible when logged in, so the sheet covers the common ones from
answers.yaml and marks anything unknown for him to answer -- never guessed.

Every value here is FICTIONAL: this file is tracked in a public repo.
"""
import json
from datetime import date

from cv_tailor import linkedin_handoff as lh

ANSWERS = {
    "years_experience": 9,
    "notice_period": "Fixture weeks",
    "languages_spoken": ["Examplish"],
    "how_heard": "Fixture board",
    "salary_fulltime_gross_eur_month": 1234,
    "start_availability_days": 14,
    "work_authorization": "Fixture citizen of Examplestan.",
    "open_to_travel": True,
    "in_person_interview": True,
    "timezone_overlap_ok": ["Exampleton"],
}
PROFILE = {"contact": {"name": "Ada Lovelace", "email": "ada@example.com",
                       "phone": "+44 20 7946 0958"}}


def test_answer_sheet_carries_the_facts_scout_holds():
    sheet = lh.answer_sheet(PROFILE, ANSWERS)
    for expected in ("Ada Lovelace", "ada@example.com", "+44 20 7946 0958",
                     "Years of experience: 9", "Fixture weeks", "Examplish",
                     "Fixture board", "1234 EUR gross per month",
                     "Within 14 days of an offer", "Examplestan", "Exampleton"):
        assert expected in sheet, expected


def test_missing_facts_are_marked_for_him_never_guessed():
    sheet = lh.answer_sheet(PROFILE, {"years_experience": 9})
    assert "Notice period: answer yourself" in sheet
    assert "Salary expectation: answer yourself" in sheet


def test_handoffs_are_counted_per_day(tmp_path):
    day = tmp_path / "2026-09-24"
    day.mkdir()
    (day / "jobs.json").write_text(json.dumps([
        {"id": "a", "status": "handed_off", "handed_off_at": "2026-09-24T08:00:00+00:00"},
        {"id": "b", "status": "handed_off", "handed_off_at": "2026-09-23T08:00:00+00:00"},
        {"id": "c", "status": "needs_human"},
    ]))
    assert lh.handoffs_on(date(2026, 9, 24), queue_dir=tmp_path) == 1


def test_the_daily_cap_is_ten_unless_overridden(monkeypatch):
    monkeypatch.delenv("LINKEDIN_HANDOFF_DAILY_CAP", raising=False)
    assert lh.daily_cap() == 10
    monkeypatch.setenv("LINKEDIN_HANDOFF_DAILY_CAP", "4")
    assert lh.daily_cap() == 4
