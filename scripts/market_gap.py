#!/usr/bin/env python3
"""Market gap report: what do the postings Scout has seen keep asking for, and
which of those does profile.yaml not back?

Reads every <queue-root>/<YYYY-MM-DD>/descriptions.json (queue root =
cv_tailor.scout_queue.queue_root, so SCOUT_QUEUE_DIR is honored), extracts each
posting's salient terms (cv_tailor.jd_terms), ranks terms by document frequency
(how many postings mention them), and marks each as supported or not by the
profile text. Read-only over the queue; writes one JSON report.

Usage:
  python scripts/market_gap.py [--top 40] [--per-jd 40] [--out PATH] [--profile PATH]

Writes  ~/clawd/var/scout/market_gap.json by default (--out overrides).
Run under the cv-tailor venv.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cv_tailor.jd_terms import extract_terms, judge_support, profile_text, term_present
from cv_tailor.profile import load_profile
from cv_tailor.scout_queue import queue_root

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_descriptions(root: Path) -> dict[str, str]:
    """job id -> JD text across every day dir; a later day wins on a repeat id."""
    out: dict[str, str] = {}
    for day in sorted(p for p in root.iterdir() if p.is_dir() and _DAY_RE.match(p.name)):
        f = day / "descriptions.json"
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"skipping unreadable {f}", file=sys.stderr)
            continue
        for job_id, text in (data or {}).items():
            if isinstance(text, str) and text.strip():
                out[job_id] = text
    return out


def _key(term: str) -> str:
    low = term.lower()
    return low[:-1] if low.endswith("s") and len(low) > 3 and not low.endswith("ss") else low


# Frequent in postings but not a skill anyone can lack.
NOT_SKILLS = {"location", "need", "needs", "feature", "features", "team", "teams", "work", "working",
              "time", "year", "years", "world", "product", "products", "business", "customer", "customers"}


def market_gap(descriptions: dict[str, str], profile: dict, *, top_n: int = 40,
               per_jd: int = 40, client=None) -> dict:
    # Count each posting once per term; "API"/"APIs"/"api" share one key and
    # report under their most common spelling.
    df: Counter = Counter()
    spellings: dict[str, Counter] = {}
    for text in descriptions.values():
        keys = {}
        for t in extract_terms(text, top_n=per_jd):
            keys.setdefault(_key(t["term"]), t["term"])
        for k, term in keys.items():
            df[k] += 1
            spellings.setdefault(k, Counter())[term] += 1
    df = Counter({spellings[k].most_common(1)[0][0]: n for k, n in df.items()})
    truth = profile_text(profile)
    total = len(descriptions)
    ranked = []
    for term, n in sorted(df.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]:
        ranked.append({
            "term": term, "postings": n,
            "share_pct": round(100 * n / total) if total else 0,
            "supported": term_present(term, truth),
        })
    # Literal misses the profile supports in meaning (verbatim evidence only),
    # and words that are not skills at all ("location", "need") drop out.
    if client is not None:
        unsupported = [r["term"] for r in ranked if not r["supported"]]
        support = judge_support(unsupported, profile, client=client)
        for r in ranked:
            if r["term"] in support:
                r["supported"] = True
                r["evidence"] = support[r["term"]]
    ranked = [r for r in ranked if r["term"].lower() not in NOT_SKILLS]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "postings": total,
        "top_terms": ranked,
        "unsupported": [r["term"] for r in ranked if not r["supported"]],
    }


def summary(report: dict) -> str:
    lines = [f"{report['postings']} postings analysed; top {len(report['top_terms'])} terms "
             f"by number of postings (* = profile.yaml does not back it):"]
    for r in report["top_terms"]:
        mark = " " if r["supported"] else "*"
        lines.append(f"  {mark} {r['term']:<32} {r['postings']:>4}  ({r['share_pct']}%)")
    lines.append(f"Unsupported: {', '.join(report['unsupported']) or 'none'}")
    return "\n".join(lines)


def _load_dotenv(path: Path = ROOT / ".env") -> None:
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--top", type=int, default=40, help="terms to report (default 40)")
    ap.add_argument("--per-jd", type=int, default=40,
                    help="salient terms taken from each posting (default 40)")
    ap.add_argument("--out", type=Path, default=None,
                    help="report path (default <queue-root>/market_gap.json)")
    ap.add_argument("--profile", type=Path, default=ROOT / "profile.yaml")
    ap.add_argument("--no-judge", action="store_true",
                    help="literal matching only (no LLM check of near misses)")
    args = ap.parse_args(argv)

    root = queue_root()
    if not root.is_dir():
        print(f"no Scout queue at {root}", file=sys.stderr)
        return 1
    descriptions = load_descriptions(root)
    if not descriptions:
        print(f"no descriptions.json content under {root}", file=sys.stderr)
        return 1
    profile = load_profile(args.profile)
    client = None
    if not args.no_judge:
        try:
            _load_dotenv()
            from cv_tailor.tailor_llm import build_azure_client
            client = build_azure_client()
        except Exception as exc:  # noqa: BLE001 -- the literal report still stands
            print(f"llm unavailable ({type(exc).__name__}); literal matching only", file=sys.stderr)
    report = market_gap(descriptions, profile, top_n=args.top, per_jd=args.per_jd, client=client)
    out = args.out or root / "market_gap.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(summary(report))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
