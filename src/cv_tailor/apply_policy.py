"""Which park reasons PROVE that no application was submitted.

Lives in the package, not in scripts/apply_approved.py, because two different
owners need the same judgement and must never disagree about it:

* scripts/apply_approved.py rolls back the ledger row it pre-inserted, at the
  moment an attempt parks;
* cv_tailor.autopilot's expiry sweep rolls back the row of a park that was
  never resolved, days later, when it rejects the entry.

Two copies of this list would drift, and the failure mode of drift is silent:
a row left behind marks a job as applied forever, and norm_key then blocks
every same-company|role sibling as a duplicate.
"""
from __future__ import annotations

# Reasons that prove no submission could have happened. Anything NOT here
# keeps its ledger row, because the attempt may have touched or even submitted
# the form.
#
# "handoff-manual: no adapter" is deliberately absent: a human was driving a
# real browser at the posting, so a genuine application is entirely possible
# and deleting the row would let a duplicate go out later.
NO_SUBMIT_REASONS = frozenset({
    "no-adapter", "missing-apply-target", "captcha", "login-required",
    "handoff-timeout: captcha not solved",
})

# Families that prove the same thing but carry a diagnostic suffix, so exact
# membership cannot match them. Every adapter verifies the resume upload
# BEFORE any field is typed (ashby) or at least before any submit click
# (greenhouse), and aborts there -- so a resume-upload-failed of any shape is
# as certain a "never submitted" as a captcha wall.
NO_SUBMIT_REASON_PREFIXES = ("resume-upload-failed",)


def proves_no_submission(reason: str) -> bool:
    """True when `reason` proves no submission happened, so a pre-inserted
    ledger row must be rolled back. Exact members first, then the
    diagnostic-carrying families."""
    reason = str(reason or "")
    return reason in NO_SUBMIT_REASONS or reason.startswith(NO_SUBMIT_REASON_PREFIXES)
