"""Which park reasons prove no application was submitted.

The asymmetry these pin is the whole point of the module: a reason that PROVES
no send rolls the pre-inserted ledger row back, and an ambiguous one KEEPS it.
Get the first wrong and a company is blocked forever for an application nobody
sent; get the second wrong and a duplicate application reaches a real employer.
"""
from cv_tailor.apply_policy import proves_no_submission


def test_unwritable_required_proves_no_submission():
    """Live everfield evidence 2026-09-16: the run aborted on the required
    "What would be your salary expectation for this role?" text box, having
    filled only name/email/resume -- the screenshot shows every question below
    it untouched. Nothing was submitted, yet the reason was outside the
    roll-back family, so the ledger row survived and norm_key now blocks every
    everfield role.

    The abort precedes any submit in all four adapters: ashby raises at 583
    inside the question loop vs _submit_and_await_confirmation at 899,
    greenhouse 408 vs its submit click at 474, lever 279 vs 478. micro1 raises
    inside a multi-step wizard so its abort CAN follow earlier Next clicks, but
    it checks _confirmed() after each one and returns `submitted` when the
    wizard actually completed -- so an abort is only ever reached on a step
    that was not the confirmed final submit. That is the same argument
    apply_policy already accepts for unanswerable-required.
    """
    assert proves_no_submission(
        "unwritable-required:What would be your salary expectation for this role?")


def test_unanswerable_and_resume_upload_families_still_prove_no_submission():
    """Regression guard: broadening the prefixes must not drop the ones the
    module already carried."""
    assert proves_no_submission("unanswerable-required:Total years of experience")
    assert proves_no_submission("resume-upload-failed: no file input found")
    assert proves_no_submission("no-adapter")
    assert proves_no_submission("captcha")


def test_ambiguous_reasons_must_keep_their_ledger_row():
    """The dangerous direction. Each of these MIGHT have submitted, so deleting
    the row would let a duplicate application go out on the next run."""
    assert not proves_no_submission(
        "no-confirmation: submission may have succeeded, VERIFY on the portal "
        "before applying manually")
    assert not proves_no_submission("handoff-manual: no adapter")
    assert not proves_no_submission("")
    assert not proves_no_submission("submit-click:Locator.click: Timeout 119422ms exceeded")
