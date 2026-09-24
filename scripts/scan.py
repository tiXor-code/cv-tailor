# scripts/scan.py
#!/usr/bin/env python3
"""Daily job-discovery scanner (v2 funnel).

Pipeline: fetch -> Gate 1 (rules) -> Gate 2 (SMB provenance) -> Gate 3 (dedup vs
SQLite + CRM) -> LLM score survivors -> write digest -> quiet Telegram (only when
new qualifying roles exist). Scoring/tailoring/CRM unchanged.

Discovery only: this script never approves or applies. scripts/autopilot.py,
which run_scan.sh runs immediately after, is the sole owner of that.

Usage: python scripts/scan.py [--min-score 6] [--max-results 10] [--dry-run]
"""
import argparse
import json
import re
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from cv_tailor.profile import load_profile
from cv_tailor.tailor_llm import build_azure_client
from cv_tailor.job_sources import fetch_all
from cv_tailor.match import rank_ai_native, score_job
from cv_tailor.digest import format_digest
from cv_tailor.telegram import format_digest_for_telegram, send_text
from cv_tailor.scout_queue import write_jobs_queue
from cv_tailor.budget import ApifyResultBudget, JSearchBudget, SerpBudget
from cv_tailor.harvest import load_harvested_sources

# Generated registry of auto-enrolled Ashby boards, relative to the repo root.
# Under data/ because it is machine-written runtime state (and gitignored);
# sources.yaml stays hand-curated.
HARVESTED_SOURCES = Path("data") / "sources_harvested.yaml"


def load_sources(root: Path | None = None) -> list[dict]:
    """The curated sources.yaml plus any auto-enrolled harvested boards.

    Harvested boards enrol with no review step, so the scan has to read them
    -- an enrolment the scan never loads buys nothing. A missing registry is
    normal (nothing harvested yet), and disabled slugs are already filtered
    out by load_harvested_sources, so a board de-enrolled for being bad
    cannot come back through this path.
    """
    root = Path(root) if root is not None else ROOT
    with open(root / "sources.yaml") as f:
        curated = yaml.safe_load(f)["sources"] or []
    return list(curated) + list(load_harvested_sources(root / HARVESTED_SOURCES))


def _is_auth_error(exc: Exception) -> bool:
    """True for a 401/invalid-key failure. Checked by shape, not by importing the
    openai SDK's exception classes, so it survives SDK changes."""
    if exc.__class__.__name__ in ("AuthenticationError", "PermissionDeniedError"):
        return True
    if getattr(exc, "status_code", None) == 401:
        return True
    blob = str(exc).lower()
    return "401" in blob and ("invalid subscription key" in blob
                             or "access denied" in blob
                             or "incorrect api key" in blob)
from cv_tailor.cache import connect, is_new, mark_seen
from cv_tailor.gates import matched_tracks, passes_gate1_tracks, requires_excluded_language
from cv_tailor.enrich import is_smb, smb_hint

DB_PATH = ROOT / "data" / "jobs.db"
BUDGET_PATH = ROOT / "data" / "serpapi_budget.json"
# JSearch has its OWN counter: 6 keys x 200 req/mo = 1200/mo, a real cap
# that nothing enforced before. A shared file would let one source spend
# the other's allowance.
JSEARCH_BUDGET_PATH = ROOT / "data" / "jsearch_budget.json"
# Apify gets its own counter too, and for a stronger reason: it is metered by
# RESULT, not by request, on a FREE $5/month tier. A shared file would let one
# source spend another's rows.
APIFY_BUDGET_PATH = ROOT / "data" / "apify_result_budget.json"


# Gate rejection buckets. GATE1_ROLE vs GATE1_GEO splits the one gate that does
# almost all of the cutting: a job whose keywords never matched was never a
# candidate (role), while a keyword-matching job dropped for remote/EU/hybrid
# reasons (geo) is a REAL match dying at the gate -- the only one of the two
# worth arguing about. A job failing both is counted once, as role.
GATE1_ROLE = "gate1_role"
GATE1_GEO = "gate1_geo"
# A real AI match dropped only because it needs German or French (Teodor,
# 2026-09-24). Its own bucket, so the cost of that rule stays visible.
GATE1_LANG = "gate1_lang"
GATE2_SMB = "gate2_smb"
GATE3_SEEN = "gate3_seen"
GATE3_BATCH = "gate3_batch"
GATES = (GATE1_ROLE, GATE1_GEO, GATE1_LANG, GATE2_SMB, GATE3_SEEN, GATE3_BATCH)
GATE_SAMPLE_CAP = 5     # titles kept per gate; gate 1 drops ~1,000/day
GATE_SAMPLE_WIDTH = 90  # per-sample character cap, so one long title can't own the log


def _log_safe(text, width: int = GATE_SAMPLE_WIDTH) -> str:
    """Collapse whitespace and truncate a job-board string for the scan log.

    Company names and titles come from strangers (anyone can post a job), and
    the scan log is a counter surface meant to be read and grepped: a newline
    inside a title would otherwise let a posting write its own
    "rejected gate1_geo: 999" line into scans/logs/<date>.log. Used by every
    log line here that quotes a posting."""
    return re.sub(r"\s+", " ", text or "").strip()[:width]


class GateStats:
    """Per-gate rejection counters + a bounded sample, collected by run_gates.

    The gates cut ~1,200 postings to ~11 a day and a gate-rejected job gets no
    seen_jobs row, so the rejected population left no trace anywhere: "are the
    gates too tight, and are real matches dying in them?" could not be
    answered. Counts are exhaustive; the sample is capped because the scan log
    (scans/logs/<date>.log, appended by run_scan.sh) is read by a human.

    Passed to run_gates as an out-parameter (unittest's `run(result)` shape) so
    the funnel keeps returning a plain survivor list to every existing caller.
    """

    def __init__(self):
        self.fetched = 0
        self.passed = 0
        self.rejected = {g: 0 for g in GATES}
        self.samples = {g: [] for g in GATES}

    def reject(self, gate: str, job) -> None:
        self.rejected[gate] += 1
        if len(self.samples[gate]) < GATE_SAMPLE_CAP:
            self.samples[gate].append(_log_safe(f"{job.org} / {job.title}"))

    def summary_lines(self) -> list[str]:
        """One header + one line per gate, always -- including gates that
        rejected nothing, because a missing line is indistinguishable from a
        gate that never ran."""
        lines = [f"  gate funnel: {self.fetched} fetched -> {self.passed} passed"]
        for gate in GATES:
            sample = " | ".join(self.samples[gate])
            lines.append(f"    rejected {gate}: {self.rejected[gate]}"
                         + (f"  e.g. {sample}" if sample else ""))
        return lines


def _min_monthly_eur() -> int | None:
    """His pay floor (answers.yaml, gitignored): his expectation IS his minimum
    (Teodor, 2026-09-24). None when unreadable -- the scorer then applies no
    pay rule rather than a guessed one."""
    try:
        from cv_tailor.answers import load_answers
        value = (load_answers() or {}).get("salary_fulltime_gross_eur_month")
        return int(value) if value else None
    except Exception as exc:  # noqa: BLE001 -- a scan must not die on the floor
        print(f"  pay floor unavailable ({type(exc).__name__}); scoring without it", file=sys.stderr)
        return None


def run_gates(jobs, tracks, conn, stats=None):
    """Gate 1 (track-aware rules) -> Gate 2 (SMB) -> Gate 3 (dedup). Each
    survivor gains a `.track` attribute set to its winning track id (see
    gates.passes_gate1_tracks). Returns survivors.

    Gate 3 also dedupes WITHIN the batch: is_new() checks the DB but mark_seen
    only runs later in the scoring loop, so N same-norm_key postings fetched in
    one scan (e.g. 4 regional variants of one Remote.com role on 2026-07-10)
    all used to pass and queue 4 packages -- then the first armed attempt's
    ledger row blocked the other 3 as "duplicate".

    `stats` is an optional GateStats collector, filled as a side effect. It
    observes only: no gate order, gate logic or gate signature changes with it,
    and run_gates returns the same survivors whether it is passed or not."""
    from cv_tailor.cache import norm_pair
    stats = stats if stats is not None else GateStats()
    stats.fetched += len(jobs)
    survivors = []
    batch_seen = set()
    for j in jobs:
        track = passes_gate1_tracks(j, tracks)
        if track is None:
            # matched_tracks is the same keyword scan Gate 1 already ran; it is
            # re-run here only for jobs the gate rejected, purely to attribute
            # the rejection. It cannot change the outcome above.
            if not matched_tracks(j, tracks):
                stats.reject(GATE1_ROLE, j)
            elif requires_excluded_language(j.title, j.description):
                stats.reject(GATE1_LANG, j)
            else:
                stats.reject(GATE1_GEO, j)
            continue
        j.track = track
        if not is_smb(j, conn):
            stats.reject(GATE2_SMB, j)
            continue
        if not is_new(conn, j):
            stats.reject(GATE3_SEEN, j)
            continue
        key = norm_pair(j.org, j.title)
        if key in batch_seen:
            stats.reject(GATE3_BATCH, j)
            continue
        batch_seen.add(key)
        survivors.append(j)
    stats.passed += len(survivors)
    return survivors


def _score_from(r: dict) -> int | None:
    """The score in a scoring response, or None when it carried none.

    `int(r.get("score", 0))` silently turned a response with no score key into
    a genuine 0, and 565 of the live DB's 1,241 rows read 0 with no way to tell
    which were the model's verdict and which were a malformed reply. None is
    stored as NULL + status 'unscored' (see cache.mark_seen), so those rows can
    be found and re-scored. A present-but-unparseable score ("high") still
    raises, keeping the scoring loop's existing failure accounting."""
    raw = r.get("score")
    return None if raw is None else int(raw)


def _scoring_outage(survivor_count: int, *, failures: int, unscored: int) -> bool:
    """True when not one survivor produced a usable score, i.e. this is an
    outage and not a quiet day. An empty output is not evidence of an empty
    input, so the scan refuses to write a queue in that case.

    Both non-results count: an exception (failures) and a response that carried
    no score at all (unscored). The latter used to be invisible -- it became a
    silent 0 -- so a model answering every job with score-less JSON looked
    exactly like "nothing good today"."""
    return survivor_count > 0 and (failures + unscored) >= survivor_count


def should_send(scored):
    return len(scored) > 0


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())


def drop_crm_tracked(jobs, tracked_keys):
    """Gate 3's CRM half (SQLite is the other). Drop jobs whose (company, role)
    already appears in the Sheets CRM. tracked_keys is a set of
    (norm_company, norm_role) tuples."""
    return [j for j in jobs if (_norm(j.org), _norm(j.title)) not in tracked_keys]


def crm_tracked_keys():
    """Read (norm_company, norm_role) pairs already tracked in the Sheets CRM.
    Returns an empty set on any failure so the scan degrades to SQLite-only dedup."""
    try:
        from cv_tailor.sheets import get_pipeline_worksheet
        rows = get_pipeline_worksheet().get_all_values()
    except Exception as e:
        print(f"warning: CRM dedup unavailable ({e}); SQLite-only", file=sys.stderr)
        return set()
    keys = set()
    for i, row in enumerate(rows):
        if i == 0 or len(row) < 2:
            continue
        keys.add((_norm(row[0]), _norm(row[1])))
    return keys


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--min-score", type=int, default=6)
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--dry-run", action="store_true", help="No Telegram, throwaway DB.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = load_profile("profile.yaml")
    # tracks: {} config drives Gate 1 track tagging. Falls back to a single
    # 'ai' track built from the legacy target_keywords list so an older
    # profile.yaml (missing the tracks: block) doesn't crash the scan.
    tracks = profile.get("tracks") or {"ai": {"keywords": profile.get("target_keywords", [])}}
    sources = load_sources()

    db = ":memory:" if args.dry_run else DB_PATH
    conn = connect(db)

    # dry-run gets a throwaway budget file (a temp dir, deleted with the OS
    # temp cleanup) so exploratory/test runs never consume from the real
    # monthly counter -- same spirit as db=":memory:" above.
    if args.dry_run:
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        budget_path = tmp / "serpapi_budget.json"
        jsearch_budget_path = tmp / "jsearch_budget.json"
        apify_budget_path = tmp / "apify_result_budget.json"
    else:
        budget_path = BUDGET_PATH
        jsearch_budget_path = JSEARCH_BUDGET_PATH
        apify_budget_path = APIFY_BUDGET_PATH
    serp_budget = SerpBudget(path=budget_path)
    jsearch_budget = JSearchBudget(path=jsearch_budget_path)

    # A dry run must be free BY CONSTRUCTION: monthly_cap=0 makes every
    # reserve() return 0, so no apify source can open a socket even if someone
    # later forgets a guard.
    apify_budget = ApifyResultBudget(
        path=apify_budget_path, monthly_cap=0 if args.dry_run else None)
    if args.dry_run:
        # Belt and braces. Redirecting a COUNTER FILE does not stop an HTTP
        # call, so also hard-disable the env and drop the paid sources.
        os.environ["APIFY_ENABLED"] = "0"
        paid = [s for s in sources if str(s.get("kind", "")).startswith("apify")]
        if paid:
            sources = [s for s in sources
                       if not str(s.get("kind", "")).startswith("apify")]
            print(f"  dry-run: dropped {len(paid)} paid apify source(s)",
                  file=sys.stderr)

    print(f"fetching {len(sources)} sources...", file=sys.stderr)
    jobs = fetch_all(sources, serp_budget=serp_budget, jsearch_budget=jsearch_budget,
                     apify_budget=apify_budget)
    print(f"  {len(jobs)} postings", file=sys.stderr)
    print(f"  apify results: {apify_budget.used()}/{apify_budget.monthly_cap} this month "
          f"(daily cap {apify_budget.daily_cap})", file=sys.stderr)
    print(f"  serpapi budget: {serp_budget.used()}/{serp_budget.monthly_cap} used this month",
          file=sys.stderr)
    print(f"  jsearch budget: {jsearch_budget.used()}/{jsearch_budget.monthly_cap} used this month",
          file=sys.stderr)

    gate_stats = GateStats()
    survivors = run_gates(jobs, tracks, conn, stats=gate_stats)
    print(f"  {len(survivors)} passed gates (remote/EU/SMB/new)", file=sys.stderr)
    # The rejected ~99% used to vanish here. run_scan.sh appends this to
    # scans/logs/<date>.log, so the breakdown is greppable per day.
    for line in gate_stats.summary_lines():
        print(line, file=sys.stderr)

    tracked = crm_tracked_keys()
    if tracked:
        before = len(survivors)
        survivors = drop_crm_tracked(survivors, tracked)
        print(f"  {len(survivors)} after CRM dedup (dropped {before - len(survivors)})", file=sys.stderr)

    client = build_azure_client()
    min_pay = _min_monthly_eur()
    scored = []
    failures = 0
    unscored = 0
    for j in survivors:
        try:
            hint = smb_hint(j, conn)
            track = getattr(j, "track", "ai")
            r = score_job(profile, j.title, f"{j.location} [{hint}]", j.description,
                          client=client, track=track, min_monthly_eur=min_pay)
            s = _score_from(r)
            if s is not None:
                s = rank_ai_native(s, j.description)
            mark_seen(conn, j, score=s)
            if s is None:
                unscored += 1
                print(f"  no score in response for {_log_safe(f'{j.org} / {j.title}')} "
                      f"(stored as unscored, not 0)", file=sys.stderr)
                continue
            if s >= args.min_score:
                scored.append({"job": j, "score": s, "reason": r.get("reason", ""),
                               "keywords": r.get("key_keywords_matched", []),
                               "track": track})
        except Exception as e:
            # A bad/expired credential fails EVERY job identically. Abort loudly instead
            # of grinding through the whole list and writing an empty queue.
            if _is_auth_error(e):
                sys.exit(
                    f"FATAL: Azure auth rejected while scoring "
                    f"({_log_safe(str(e), width=300)}). The scan cannot score any "
                    f"job, so it is aborting WITHOUT writing a queue (an empty queue is "
                    f"indistinguishable from 'no good jobs today'). Check AZURE_OPENAI_API_KEY "
                    f"in {ROOT / '.env'} -- the key was rotated on 2026-07-09."
                )
            failures += 1
            # The exception text is untrusted too: score_job is fed j.description,
            # and an HTTP/JSON error can echo response text verbatim. int()'s
            # ValueError happens to repr()-escape newlines; nothing guarantees
            # every exception reachable here does.
            print(f"  score failed {_log_safe(f'{j.org} / {j.title}')}: "
                  f"{_log_safe(str(e), width=300)}", file=sys.stderr)

    # An empty output is not evidence of an empty input. If nothing produced a
    # usable score, this is an outage, not a quiet day.
    if _scoring_outage(len(survivors), failures=failures, unscored=unscored):
        sys.exit(
            f"FATAL: no score came back for any of the {len(survivors)} job(s) "
            f"({failures} threw, {unscored} returned no score). Refusing to write an empty "
            f"queue that would look like a normal no-results day. See the errors above."
        )
    if failures or unscored:
        print(f"  WARNING: {failures} job(s) failed to score, {unscored} returned no score "
              f"(kept {len(scored)})", file=sys.stderr)

    scored.sort(key=lambda s: s["score"], reverse=True)
    scored = scored[: args.max_results]

    today = date.today()
    scans_dir = ROOT / "scans"
    scans_dir.mkdir(exist_ok=True)
    md = format_digest(scored, scan_date=today)
    (scans_dir / f"{today.isoformat()}.md").write_text(md)
    (scans_dir / f"{today.isoformat()}.json").write_text(json.dumps(
        [{"score": s["score"], "reason": s["reason"], "keywords": s["keywords"],
          "job": {"source": s["job"].source, "org": s["job"].org, "title": s["job"].title,
                  "location": s["job"].location, "url": s["job"].url, "raw_id": s["job"].raw_id}}
         for s in scored], indent=2))

    queue_path = write_jobs_queue(scored, today)
    print(f"scout queue: {queue_path}", file=sys.stderr)
    # The queue is where this script's job ends. Approving and applying belong
    # to scripts/autopilot.py, which run_scan.sh runs next: it has the score
    # floor, compare-and-swap approval, a 7-day retry window, the stranded-run
    # and expiry sweeps, and the digest.

    print(f"\n{len(scored)} candidates >= {args.min_score}", file=sys.stderr)
    if args.dry_run:
        print(md)
        return
    if should_send(scored):
        tg = format_digest_for_telegram(scored, today.isoformat())
        print("telegram:", "sent" if send_text(tg) else "skipped", file=sys.stderr)
    else:
        print("telegram: quiet (no new qualifying roles today)", file=sys.stderr)


if __name__ == "__main__":
    main()
