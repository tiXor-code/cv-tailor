"""Application outcomes and the tracker report.

An entry that reached an employer (status `sent`: Scout submitted it, or
`applied_by_hand`: Teodor did) carries an `outcome` that moves as the employer
answers. Until anything is recorded the effective outcome is `no_reply`.

Fields written on a jobs.json entry (all through scout_queue.update_entry):
  outcome          one of OUTCOMES
  outcome_at       UTC ISO-8601 of the last outcome change
  outcome_history  [{"outcome", "at", "note"?}, ...] oldest first
  followed_up_at   UTC ISO-8601 when he chased the employer (takes it off the
                   follow-up list)

The report reads every day directory under queue_root() and never writes the
queue.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from cv_tailor.scout_queue import queue_root, update_entry

OUTCOMES = ("no_reply", "replied", "interview", "rejected", "offer", "withdrawn")
# Any answer from the employer, a rejection included.
RESPONDED = ("replied", "interview", "rejected", "offer")
POSITIVE = ("replied", "interview", "offer")
APPLIED_STATUSES = ("sent", "applied_by_hand")
# Waiting on someone: parked for Teodor, or on his apply-yourself list.
WAITING_STATUSES = ("needs_human", "needs_review", "ready", "handed_off")
FOLLOW_UP_AFTER_DAYS = 8
STALE_AFTER_DAYS = 5
NOTE_MAX = 500
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class NotApplied(Exception):
    """The entry never reached an employer, so it has no outcome to track."""

    def __init__(self, job_id: str, status):
        self.job_id = job_id
        self.status = status
        super().__init__(f"job {job_id!r} has status {status!r}, not one of {APPLIED_STATUSES}")


def _utc(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now if now.tzinfo else now.replace(tzinfo=timezone.utc)


def _parse_ts(raw) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def clean_note(note) -> str | None:
    if note is None:
        return None
    text = _CTRL_RE.sub(" ", str(note)).strip()
    return text[:NOTE_MAX] or None


def effective_outcome(entry: dict) -> str | None:
    """The entry's outcome, `no_reply` for an application nothing was recorded
    on yet, None for an entry that never reached an employer."""
    if entry.get("status") not in APPLIED_STATUSES:
        return None
    out = entry.get("outcome")
    return out if out in OUTCOMES else "no_reply"


def set_outcome(scan_date: str, job_id: str, outcome: str | None = None, *,
                note=None, followed_up: bool = False, now: datetime | None = None,
                queue_dir=None) -> tuple[dict, str | None]:
    """Record an outcome (and/or a follow-up) on an applied entry.

    Returns (entry, previous_outcome). Raises ValueError on an unknown outcome,
    NotApplied when the entry never reached an employer (checked inside the
    queue lock), KeyError / FileNotFoundError for an unknown job or day."""
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; expected one of {OUTCOMES}")
    if outcome is None and not followed_up and note is None:
        raise ValueError("nothing to record")
    stamp = _utc(now).isoformat()
    note = clean_note(note)
    seen: dict = {}

    def _mut(e: dict) -> None:
        if e.get("status") not in APPLIED_STATUSES:
            raise NotApplied(job_id, e.get("status"))
        previous = effective_outcome(e)
        seen["previous"] = previous
        history = list(e.get("outcome_history") or [])
        if outcome is not None and outcome != previous:
            e["outcome"] = outcome
            e["outcome_at"] = stamp
            row = {"outcome": outcome, "at": stamp}
            if note:
                row["note"] = note
            history.append(row)
        elif note:
            # Same outcome (or a follow-up only): keep the note, dated.
            history.append({"outcome": previous, "at": stamp, "note": note})
        if followed_up:
            e["followed_up_at"] = stamp
            if not note:
                history.append({"outcome": previous, "at": stamp, "note": "followed up"})
        e.setdefault("outcome", previous)
        e["outcome_history"] = history

    entry = update_entry(scan_date, job_id, _mut, queue_dir=queue_dir)
    return entry, seen.get("previous")


# --- reading the queue -------------------------------------------------------

def iter_entries(queue_dir=None):
    """(scan_date, entry) for every entry in every day directory, oldest day first."""
    root = queue_root(queue_dir)
    if not root.exists():
        return
    for day_dir in sorted(p for p in root.iterdir() if p.is_dir() and _DAY_RE.match(p.name)):
        path = day_dir / "jobs.json"
        if not path.exists():
            continue
        try:
            entries = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                yield day_dir.name, e


def applied_on(entry: dict, scan_date: str) -> datetime:
    for key in ("applied_at", "status_changed_at", "decided_at"):
        ts = _parse_ts(entry.get(key))
        if ts:
            return ts
    return datetime.fromisoformat(scan_date).replace(tzinfo=timezone.utc)


def last_change(entry: dict, scan_date: str) -> datetime:
    for key in ("status_changed_at", "handed_off_at", "decided_at"):
        ts = _parse_ts(entry.get(key))
        if ts:
            return ts
    return datetime.fromisoformat(scan_date).replace(tzinfo=timezone.utc)


def channel(entry: dict) -> str:
    return "scout" if entry.get("status") == "sent" else "by_hand"


def score_band(score) -> str:
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "unscored"
    if s >= 9:
        return "9-10"
    if s >= 7:
        return "7-8"
    if s >= 5:
        return "5-6"
    return "0-4"


def cv_variant(entry: dict) -> str:
    """Which CV went out. meta.json `variant` / `cv_variant` / `variants.chosen`
    when the package records one, else the chosen CV file's stem, else "cv"."""
    meta = {}
    pkg = entry.get("package_dir")
    if pkg:
        try:
            meta = json.loads((Path(pkg) / "meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
    for key in ("variant", "cv_variant"):
        if isinstance(meta.get(key), str) and meta[key].strip():
            return meta[key].strip()
    variants = meta.get("variants")
    if isinstance(variants, dict):
        chosen = variants.get("chosen")
        if isinstance(chosen, str) and chosen.strip():
            return Path(chosen.strip()).stem
    cv_path = entry.get("cv_path")
    if isinstance(cv_path, str) and cv_path:
        stem = Path(cv_path).stem
        if stem and stem != "cv":
            return stem
    return "cv"


def follow_ups_due(now: datetime | None = None, *, queue_dir=None,
                   after_days: int = FOLLOW_UP_AFTER_DAYS) -> list[dict]:
    """Applications with no reply `after_days` or more after they went out and
    not yet chased, freshest first (the best ones to chase lead)."""
    now = _utc(now)
    due = []
    for scan_date, e in iter_entries(queue_dir):
        if effective_outcome(e) != "no_reply" or e.get("followed_up_at"):
            continue
        days = (now - applied_on(e, scan_date)).days
        if days >= after_days:
            due.append({"scan_date": scan_date, "id": e.get("id"),
                        "company": e.get("company"), "title": e.get("title"),
                        "channel": channel(e), "days_since_applied": days,
                        "url": e.get("url")})
    due.sort(key=lambda d: (d["days_since_applied"], d["company"] or ""))
    return due


def _rates(groups: dict) -> dict:
    out = {}
    for key, outcomes in sorted(groups.items()):
        n = len(outcomes)
        responded = sum(1 for o in outcomes if o in RESPONDED)
        positive = sum(1 for o in outcomes if o in POSITIVE)
        out[key] = {"applied": n, "responded": responded, "positive": positive,
                    "response_rate": round(responded / n, 3) if n else 0.0}
    return out


def build_report(now: datetime | None = None, *, queue_dir=None,
                 follow_up_days: int = FOLLOW_UP_AFTER_DAYS,
                 stale_days: int = STALE_AFTER_DAYS) -> dict:
    now = _utc(now)
    statuses: Counter = Counter()
    outcomes: Counter = Counter()
    by: dict = {"variant": defaultdict(list), "source": defaultdict(list),
                "channel": defaultdict(list), "score_band": defaultdict(list)}
    stale = []
    for scan_date, e in iter_entries(queue_dir):
        statuses[e.get("status") or "unknown"] += 1
        out = effective_outcome(e)
        if out is not None:
            outcomes[out] += 1
            by["variant"][cv_variant(e)].append(out)
            by["source"][e.get("source") or "unknown"].append(out)
            by["channel"][channel(e)].append(out)
            by["score_band"][score_band(e.get("score"))].append(out)
        elif e.get("status") in WAITING_STATUSES:
            days = (now - last_change(e, scan_date)).days
            if days >= stale_days:
                stale.append({"scan_date": scan_date, "id": e.get("id"),
                              "company": e.get("company"), "title": e.get("title"),
                              "status": e.get("status"), "days_waiting": days,
                              "reason": e.get("error") or e.get("handoff_reason") or ""})
    stale.sort(key=lambda d: -d["days_waiting"])
    applied = sum(outcomes.values())
    responded = sum(outcomes[o] for o in RESPONDED)
    return {
        "generated_at": now.isoformat(),
        "totals": {"entries": sum(statuses.values()), "applied": applied,
                   "responded": responded,
                   "response_rate": round(responded / applied, 3) if applied else 0.0},
        "status_breakdown": dict(statuses.most_common()),
        "outcome_breakdown": {o: outcomes[o] for o in OUTCOMES if outcomes[o]},
        "response_rate": {k: _rates(v) for k, v in by.items()},
        "follow_ups_due": follow_ups_due(now, queue_dir=queue_dir, after_days=follow_up_days),
        "stale_waiting": stale,
    }


def format_report(report: dict) -> str:
    t = report["totals"]
    lines = [f"Scout tracker - {report['generated_at'][:10]}",
             f"{t['entries']} entries, {t['applied']} applied, {t['responded']} responses "
             f"({t['response_rate']:.0%})", "", "Status:"]
    lines += [f"  {k}: {v}" for k, v in report["status_breakdown"].items()]
    if report["outcome_breakdown"]:
        lines += ["", "Outcomes:"]
        lines += [f"  {k}: {v}" for k, v in report["outcome_breakdown"].items()]
    for dim, rows in report["response_rate"].items():
        if not rows:
            continue
        lines += ["", f"Response rate by {dim.replace('_', ' ')}:"]
        for key, r in rows.items():
            lines.append(f"  {key}: {r['responded']}/{r['applied']} ({r['response_rate']:.0%}),"
                         f" {r['positive']} positive")
    due = report["follow_ups_due"]
    lines += ["", f"Follow-ups due ({len(due)}):"]
    lines += [f"  {d['company']} / {d['title']}: {d['days_since_applied']} days ({d['channel']})"
              for d in due] or ["  none"]
    stale = report["stale_waiting"]
    lines += ["", f"Waiting and going stale ({len(stale)}):"]
    lines += [f"  {s['company']} / {s['title']}: {s['status']} {s['days_waiting']} days"
              for s in stale] or ["  none"]
    return "\n".join(lines)
