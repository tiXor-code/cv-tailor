"""Screening answers loader with required-key validation.

The screening answers file contains standardized responses to common job
application questions (salary expectations, availability, work authorization,
etc.) so that tailored cover letters and portals can reference them.
"""
from pathlib import Path
import yaml


REQUIRED_KEYS = {
    "salary_fulltime_gross_eur_month",
    "salary_fulltime_net_eur_month",
    "hourly_rate_min_eur",
    "availability_parttime",
    "work_authorization",
    "notice_period",
    "relocation",
    "links",
}


class AnswersError(ValueError):
    """Raised when answers.yaml is missing required keys or cannot be loaded."""

    def __init__(self, message: str):
        super().__init__(message)


def load_answers(path: Path | str | None = None) -> dict:
    """Load screening answers from a YAML file.

    If path is None, defaults to ROOT/answers.yaml where ROOT is the
    repository root (parent's parent of this module).

    Raises AnswersError if:
    - The file does not exist
    - Any required key is missing

    Returns the parsed YAML dict with all keys intact (no defaults).
    """
    if path is None:
        root = Path(__file__).resolve().parent.parent.parent
        path = root / "answers.yaml"
    else:
        path = Path(path)

    # Check if file exists
    if not path.exists():
        raise AnswersError(
            f"Answers file not found: {path}\n"
            f"Copy answers.example.yaml to answers.yaml and fill in your values."
        )

    # Load YAML
    try:
        with open(path, "r", encoding="utf-8") as f:
            answers = yaml.safe_load(f)
    except Exception as e:
        raise AnswersError(f"Failed to parse {path}: {e}")

    if not isinstance(answers, dict):
        raise AnswersError(f"Answers must be a YAML dict, got {type(answers).__name__}")

    # Validate required keys
    missing = REQUIRED_KEYS - set(answers.keys())
    if missing:
        raise AnswersError(
            f"Answers is missing required keys: {', '.join(sorted(missing))}"
        )

    return answers
