import pytest
from cv_tailor.answers import load_answers, AnswersError


def test_load_answers_returns_dict_with_all_keys(fixtures_dir):
    """Load a fixture with all required keys -> dict returned intact."""
    answers = load_answers(fixtures_dir / "answers_complete.yaml")
    assert isinstance(answers, dict)
    assert "salary_fulltime_gross_eur_month" in answers
    assert "salary_fulltime_net_eur_month" in answers
    assert "hourly_rate_min_eur" in answers
    assert "availability_parttime" in answers
    assert "work_authorization" in answers
    assert "notice_period" in answers
    assert "relocation" in answers
    assert "links" in answers


def test_load_answers_raises_when_missing_relocation(fixtures_dir):
    """Missing required key 'relocation' -> AnswersError with key name in message."""
    with pytest.raises(AnswersError) as exc_info:
        load_answers(fixtures_dir / "answers_missing_relocation.yaml")
    assert "relocation" in str(exc_info.value)


def test_load_answers_raises_when_file_missing(tmp_path):
    """Missing file -> AnswersError with hint to copy answers.example.yaml."""
    missing = tmp_path / "answers.yaml"
    with pytest.raises(AnswersError) as exc_info:
        load_answers(missing)
    error_msg = str(exc_info.value)
    assert "answers.yaml" in error_msg
    assert "answers.example.yaml" in error_msg
