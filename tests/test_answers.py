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


# --- Answers he types into a form, saved by Scout Fill (2026-09-25) --------

def _answers_file(tmp_path, extra=""):
    import textwrap
    p = tmp_path / "answers.yaml"
    p.write_text(textwrap.dedent("""\
        salary_fulltime_gross_eur_month: 1234
        salary_fulltime_net_eur_month: "900"
        hourly_rate_min_eur: 10
        availability_parttime: fixture
        work_authorization: fixture
        notice_period: fixture weeks
        relocation: fixture
        links: {}
        question_answers:
          - match: "Hand written question"
            answer: "Hand written answer."
        """) + extra)
    return p


def test_saved_answers_are_merged_after_his_hand_written_ones(tmp_path):
    from cv_tailor.answers import load_answers, save_question_answers
    p = _answers_file(tmp_path)
    save_question_answers([{"label": "Why fixtures?*", "value": "Because."}], path=p)
    qa = load_answers(p)["question_answers"]
    assert [x["match"] for x in qa] == ["Hand written question", "Why fixtures?"]
    assert qa[1]["answer"] == "Because."


def test_saving_the_same_question_again_updates_it(tmp_path):
    from cv_tailor.answers import load_answers, save_question_answers
    p = _answers_file(tmp_path)
    save_question_answers([{"label": "Why fixtures?", "value": "First."}], path=p)
    save_question_answers([{"label": "WHY fixtures", "value": "Second."}], path=p)
    qa = load_answers(p)["question_answers"]
    assert [x["answer"] for x in qa if "fixtures" in x["match"].lower()] == ["Second."]


def test_saved_answers_are_capped_and_empty_ones_skipped(tmp_path):
    from cv_tailor.answers import load_answers, save_question_answers, SAVED_MAX_VALUE
    p = _answers_file(tmp_path)
    n = save_question_answers([{"label": "Long?", "value": "x" * 20000},
                               {"label": "Empty?", "value": "   "},
                               {"label": "", "value": "orphan"}], path=p)
    assert n == 1
    qa = load_answers(p)["question_answers"]
    assert len(qa[-1]["answer"]) == SAVED_MAX_VALUE


def test_the_saved_file_is_private(tmp_path):
    import stat
    from cv_tailor.answers import save_question_answers, saved_answers_path
    p = _answers_file(tmp_path)
    save_question_answers([{"label": "Q?", "value": "A."}], path=p)
    assert stat.S_IMODE(saved_answers_path(p).stat().st_mode) == 0o600
