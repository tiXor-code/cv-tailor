"""Leak canary: the repo is PUBLIC. Teodor's real screening answers live ONLY in
the gitignored answers.yaml. This test fails if any sensitive value from the
real answers.yaml appears in any git-TRACKED file, so an agent embedding real
data as a test fixture (it has happened three times) breaks the suite before
commit instead of after push. Skips when answers.yaml is absent (CI, clones)."""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ANSWERS = ROOT / "answers.yaml"


def _sensitive_strings(data):
    vals = []
    for key in ("salary_fulltime_gross_eur_month", "salary_fulltime_net_eur_month",
                "hourly_rate_min_eur", "availability_parttime", "notice_period"):
        v = data.get(key)
        if v is not None:
            vals.append(str(v))
    wa = str(data.get("work_authorization", ""))
    if wa:
        # the distinctive tail, not the generic EU-citizen opener
        vals.append(wa.strip().splitlines()[-1].strip()[-40:])

    # start_availability_days / hourly_rate_ask_usd / hours_per_week_available
    # (micro1's post-Next start/rate/hours step, see answers.py) are short
    # numbers (e.g. "7", "40", "20") -- the len(v) >= 4 filter below and the
    # word-isolation check in test_real_answers_never_in_tracked_files would
    # either drop them outright or false-fire on any unrelated short number
    # (a timeout, a loop count) that happens to equal one of them. Guard the
    # KEY: VALUE pairing itself instead -- long enough to pass the length
    # filter and specific enough that only a real fixture copy-paste of the
    # actual answers.yaml line (not a coincidental bare number) trips it.
    for key in ("start_availability_days", "hourly_rate_ask_usd", "hours_per_week_available"):
        v = data.get(key)
        if v is not None:
            vals.append(f"{key}: {v}")

    return [v for v in vals if len(v) >= 4]


@pytest.mark.skipif(not ANSWERS.exists(), reason="no real answers.yaml on this machine")
def test_real_answers_never_in_tracked_files():
    data = yaml.safe_load(ANSWERS.read_text())
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.splitlines()
    leaks = []
    for rel in tracked:
        p = ROOT / rel
        try:
            text = p.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for v in _sensitive_strings(data):
            if v.isdigit():
                # pure numbers need word isolation (a salary figure must not flag a timeout-in-ms string)
                if re.search(rf"(?<![\w.]){re.escape(v)}(?![\w])", text):
                    leaks.append(f"{rel}: {v!r}")
            elif v in text:
                leaks.append(f"{rel}: {v!r}")
    assert not leaks, "REAL answers.yaml values found in tracked files:\n" + "\n".join(leaks)
