# tests/test_scan_funnel.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import scan
from cv_tailor.cache import connect
from cv_tailor.job_sources import JobPosting


def _job(source, raw_id, title, location, desc=""):
    return JobPosting(source=source, org=f"Co{raw_id}", title=title, location=location,
                      url="https://x", description=desc, raw_id=raw_id)


def test_funnel_filters_and_dedupes(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    tracks = {"ai": {"keywords": ["ai engineer", "python"]}}
    jobs = [
        _job("greenhouse", "1", "AI Engineer", "Remote - EU", "Python"),        # passes
        _job("greenhouse", "2", "Account Executive", "Remote - EU", "sales"),   # fails gate1 (role)
        _job("greenhouse", "3", "AI Engineer", "Remote - US only", "Python"),   # fails gate1 (geo)
        _job("workday",    "4", "AI Engineer", "Remote - EU", "Python"),        # fails gate2 (enterprise)
    ]
    survivors = scan.run_gates(jobs, tracks, conn)
    assert [j.raw_id for j in survivors] == ["1"]
    assert survivors[0].track == "ai"

    # Mark #1 seen, re-run: now deduped out.
    from cv_tailor.cache import mark_seen
    mark_seen(conn, jobs[0], score=9)
    assert scan.run_gates(jobs, tracks, conn) == []


def test_funnel_tags_winning_track():
    conn = connect(":memory:")
    tracks = {
        "ai": {"keywords": ["ai engineer"]},
        "content": {"keywords": ["content producer"]},
    }
    jobs = [
        _job("greenhouse", "5", "Content Producer", "Remote - EU", "content producer role"),
        _job("greenhouse", "6", "AI Content Producer", "Remote - EU", "ai engineer and content producer"),
    ]
    survivors = scan.run_gates(jobs, tracks, conn)
    by_id = {j.raw_id: j for j in survivors}
    assert by_id["5"].track == "content"
    assert by_id["6"].track == "ai"  # tie -> ai wins config order


def test_quiet_digest_decides_send():
    assert scan.should_send([]) is False
    assert scan.should_send([{"score": 8}]) is True


def test_drop_crm_tracked():
    jobs = [
        _job("greenhouse", "1", "AI Engineer", "Remote - EU"),
        _job("lever", "2", "Backend Engineer", "Remote - EU"),
    ]
    # "Co1" / "AI Engineer" already tracked (normalized) -> dropped; whitespace/case-insensitive.
    tracked = {("co1", "aiengineer")}
    kept = scan.drop_crm_tracked(jobs, tracked)
    assert [j.raw_id for j in kept] == ["2"]
    # empty tracked set keeps everything
    assert len(scan.drop_crm_tracked(jobs, set())) == 2


# --- autopilot is the sole apply owner ----------------------------------

def test_scan_owns_no_apply_path():
    """scan.py discovers, scores and writes the queue -- then stops.

    It used to also approve and apply (auto_apply_pending): today's queue only,
    EVERY pending entry regardless of score, no compare-and-swap. Because it ran
    inside the scan it drained the queue before scripts/autopilot.py ever looked,
    so autopilot's score floor was dead config and nothing enforced a policy
    threshold on real sends. Applying now belongs to autopilot alone. Any of
    these names or spawns coming back re-creates that race."""
    for name in ("auto_apply_pending", "_auto_apply_enabled", "_default_apply_runner"):
        assert not hasattr(scan, name), (
            f"scan.{name} is back -- applying belongs to scripts/autopilot.py alone")
    src = pathlib.Path(scan.__file__).read_text()
    assert "apply_approved" not in src, "scan.py must not spawn the apply orchestrator"
    assert "AUTO_APPLY" not in src, "the AUTO_APPLY gate is retired, not re-read"


def test_queue_threshold_is_six_at_both_sites():
    """The queue-entry threshold moved 7 -> 6, and it lives in TWO places: the
    argparse default here, and the --min-score run_scan.sh passes explicitly at
    09:00 under launchd. The explicit flag wins, so changing only the default is
    a silent no-op in production -- which is exactly why the launchd wrapper is
    asserted here and not just the parser."""
    assert scan.parse_args([]).min_score == 6

    wrapper = (pathlib.Path(scan.__file__).parent / "run_scan.sh").read_text()
    invocations = [ln for ln in wrapper.splitlines()
                   if "scan.py" in ln and not ln.lstrip().startswith("#")]
    assert invocations, "run_scan.sh no longer runs scan.py"
    for line in invocations:
        assert "--min-score" not in line or "--min-score 6" in line, (
            f"run_scan.sh overrides the threshold with a stale value: {line.strip()}")


# --- a missing score is not a zero ---------------------------------------

def test_score_from_reads_a_real_score():
    assert scan._score_from({"score": 7}) == 7
    assert scan._score_from({"score": "7"}) == 7   # the model sometimes stringifies


def test_score_from_zero_is_a_real_zero():
    """The model rating a job 0 is a verdict and must stay a 0."""
    assert scan._score_from({"score": 0}) == 0


def test_score_from_missing_or_null_score_is_none():
    """r.get("score", 0) turned "the response had no score key" into a genuine
    0 -- 565 of the live DB's 1,241 rows read 0 and nothing can tell the two
    apart. An absent (or null) score is now None, stored as NULL + status
    'unscored', so it can be re-scored later."""
    assert scan._score_from({"reason": "no score key at all"}) is None
    assert scan._score_from({"score": None}) is None
    assert scan._score_from({}) is None


def test_scoring_outage_counts_unscored_as_a_failure():
    """"An empty output is not evidence of an empty input": the scan aborts
    rather than write a queue that looks like a quiet day when NO survivor
    produced a usable score. A response with no score key is such a
    non-result, so it belongs in that count -- otherwise a model returning
    valid JSON with no score for every job would have looked like "no good
    jobs today" (before this change those jobs silently became 0s, which the
    old failures-only count also missed)."""
    assert scan._scoring_outage(3, failures=3, unscored=0) is True
    assert scan._scoring_outage(3, failures=0, unscored=3) is True
    assert scan._scoring_outage(3, failures=1, unscored=2) is True
    # one survivor produced a real score -> a normal day, however bad the score
    assert scan._scoring_outage(3, failures=1, unscored=1) is False
    # nothing reached the scorer at all -> not an outage, just an empty funnel
    assert scan._scoring_outage(0, failures=0, unscored=0) is False


def test_score_from_unparseable_score_still_raises():
    """A score of "high" is a malformed response, not an unscored job: it keeps
    raising so the scoring loop counts it as a failure exactly as before."""
    import pytest
    with pytest.raises(ValueError):
        scan._score_from({"score": "high"})


def test_funnel_dedupes_same_norm_key_within_one_batch(tmp_path):
    """2026-07-10 regression: 4 regional variants of one Remote.com role share a
    norm_key (region is stripped) and all passed Gate 3 in a single scan, so all
    four queued and the first attempt's ledger row blocked the other three as
    "duplicate". Only ONE variant may survive a batch."""
    conn = connect(tmp_path / "jobs.db")
    tracks = {"ai": {"keywords": ["ai engineer", "python"]}}
    variants = [
        _job("greenhouse", "r1", "AI Engineer", "Remote - EMEA", "Python"),
        _job("greenhouse", "r2", "AI Engineer", "Remote - Northern EU", "Python"),
        _job("greenhouse", "r3", "AI Engineer", "Remote - Southern EU", "Python"),
    ]
    # same org so the norm_key collides
    for v in variants:
        v.org = "Remote.com"
    survivors = scan.run_gates(variants, tracks, conn)
    assert len(survivors) == 1
    assert survivors[0].raw_id == "r1"
