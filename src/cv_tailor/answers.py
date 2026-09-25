"""Screening answers loader with required-key validation.

The screening answers file contains standardized responses to common job
application questions (salary expectations, availability, work authorization,
etc.) so that tailored cover letters and portals can reference them.
"""
import os
import re
import tempfile
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

    # Answers he typed into forms and saved via Scout Fill live in a sibling
    # file, so answers.yaml (hand-edited, commented) is never rewritten. His
    # hand-written entries come first and therefore win on a shared question.
    saved = _read_saved(saved_answers_path(path))
    if saved:
        answers["question_answers"] = list(answers.get("question_answers") or []) + saved
    return answers


# --- Saved answers (Scout Fill, 2026-09-25) ---------------------------------
SAVED_MAX_ITEMS = 30          # per save call
SAVED_MAX_LABEL = 500
SAVED_MAX_VALUE = 4000


def saved_answers_path(answers_path: Path | str) -> Path:
    return Path(answers_path).with_name("answers_saved.yaml")


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def _read_saved(path: Path) -> list:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except (OSError, yaml.YAMLError):
        return []
    return [x for x in data if isinstance(x, dict) and x.get("match") and x.get("answer")] if isinstance(data, list) else []


def save_question_answers(items, path: Path | str | None = None) -> int:
    """Save answers he typed into a form (label -> value) for reuse on any
    form that asks the same question. The label's trailing required-marker is
    dropped; a question saved again is updated (his latest answer wins).
    Returns how many were saved. Writes answers_saved.yaml atomically, 0600."""
    if path is None:
        path = Path(__file__).resolve().parent.parent.parent / "answers.yaml"
    target = saved_answers_path(path)
    current = _read_saved(target)
    index = {_norm(x["match"]): i for i, x in enumerate(current)}
    saved = 0
    for item in list(items or [])[:SAVED_MAX_ITEMS]:
        label = re.sub(r"\s*\*\s*$", "", str((item or {}).get("label") or "")).strip()[:SAVED_MAX_LABEL]
        value = str((item or {}).get("value") or "").strip()[:SAVED_MAX_VALUE]
        if not _norm(label) or not value:
            continue
        entry = {"match": label, "answer": value}
        key = _norm(label)
        if key in index:
            current[index[key]] = entry
        else:
            index[key] = len(current)
            current.append(entry)
        saved += 1
    if saved:
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".answers_saved.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("# Answers Teodor typed into application forms and saved via Scout Fill.\n"
                         "# Reused on any form asking the same question. Edit freely.\n")
                yaml.safe_dump(current, fh, sort_keys=False, allow_unicode=True, width=1000)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
    return saved
