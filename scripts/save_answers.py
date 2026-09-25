#!/usr/bin/env python3
"""Save answers Teodor typed into an application form (via Scout Fill) so
Scout reuses them on any form asking the same question.

stdin : {"answers": [{"label": "...", "value": "..."}]}
stdout: {"saved": N}
Input comes from a web page via the extension: untrusted, capped in
cv_tailor.answers.save_question_answers. Exit 2 on bad input.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.answers import save_question_answers  # noqa: E402


def main(argv=None, *, path=None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 2
    items = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        return 2
    json.dump({"saved": save_question_answers(items, path=path)}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
