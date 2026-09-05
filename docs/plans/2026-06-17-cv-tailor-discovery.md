# CV Tailor v2 Discovery — Phase 1 Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert a cost-ordered filter funnel (cheap rules → SMB provenance gate → SQLite dedup → existing LLM scorer) between job fetching and scoring, add Greenhouse + Lever as new source kinds, and deliver a daily quiet Telegram digest — proving the v2 architecture end-to-end on free sources.

**Architecture:** Sources fetch into the existing `JobPosting` dataclass. A new `gates.py` (free rules), `enrich.py` (provenance SMB verdict), and `cache.py` (SQLite cross-source dedup) sit between `fetch_all()` and the existing `score_job()`. A new `scripts/scan.py` orchestrates the funnel and only Telegrams when new qualifying roles exist. Scoring, CV tailoring, and the Sheets CRM are untouched.

**Tech Stack:** Python 3.11+, stdlib `urllib`/`sqlite3`/`json`, PyYAML, Azure OpenAI (existing), pytest.

**Spec:** `docs/specs/2026-06-17-cv-tailor-discovery-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/cv_tailor/cache.py` | **new** — SQLite `seen_jobs` + `company_enrichment` tables; cross-source dedup helpers |
| `src/cv_tailor/gates.py` | **new** — Gate 1 pure rules: remote / EU-eligible / target-keyword |
| `src/cv_tailor/enrich.py` | **new** — Gate 2 provenance SMB verdict (Hunter deferred to Phase 2) |
| `src/cv_tailor/job_sources.py` | **modify** — add `fetch_greenhouse_org`, `fetch_lever_org`; extend `fetch_all` dispatch |
| `profile.yaml` | **modify** — add explicit `target_keywords` block |
| `sources.yaml` | **modify** — add a couple of greenhouse/lever sources |
| `scripts/scan.py` | **new** — funnel orchestration + quiet digest (replaces `weekly_scan.py`) |
| `scripts/run_scan.sh` | **new** — launchd entrypoint (replaces `run_weekly_scan.sh`) |
| `~/Library/LaunchAgents/com.teodorlutoiu.cvtailor.daily.plist` | **new** — daily ~09:00 |
| `~/aios/health/registry.yaml` | **modify** — repoint `cv-tailor` entry to the new label |
| `tests/test_cache.py`, `tests/test_gates.py`, `tests/test_enrich.py`, `tests/test_job_sources_v2.py`, `tests/test_scan_funnel.py` | **new** tests |

Untouched: `tailor_llm.py`, `scripts/tailor.py`, `render.py`, `ats_check.py`, `sheets.py`, `scripts/process_approved.py`, `digest.py`, `telegram.py`, `match.py`.

---

## Task 1: SQLite cache + cross-source dedup

**Files:**
- Create: `src/cv_tailor/cache.py`
- Test: `tests/test_cache.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cache.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.cache import connect, is_new, mark_seen
from cv_tailor.job_sources import JobPosting


def _job(source="greenhouse", raw_id="1", org="Acme", title="AI Engineer"):
    return JobPosting(source=source, org=org, title=title, location="Remote (EU)",
                      url="https://x", description="desc", raw_id=raw_id)


def test_new_then_seen(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    j = _job()
    assert is_new(conn, j) is True
    mark_seen(conn, j, score=8)
    assert is_new(conn, j) is False


def test_cross_source_dedup_by_company_role(tmp_path):
    conn = connect(tmp_path / "jobs.db")
    a = _job(source="greenhouse", raw_id="1", org="Acme Inc.", title="AI Engineer")
    mark_seen(conn, a, score=8)
    # Same company+role from a different board / id is NOT new.
    b = _job(source="serpapi", raw_id="zzz", org="acme inc", title="AI  Engineer")
    assert is_new(conn, b) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/repos/cv-tailor && python -m pytest tests/test_cache.py -v`
Expected: FAIL with `ModuleNotFoundError: cv_tailor.cache`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cv_tailor/cache.py
"""SQLite cache: cross-source dedup (seen_jobs) + enrichment cache (Phase 2)."""
from __future__ import annotations
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    source TEXT, raw_id TEXT, company TEXT, role TEXT, location TEXT,
    norm_key TEXT, first_seen TEXT, score INTEGER, status TEXT,
    PRIMARY KEY (source, raw_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_norm ON seen_jobs(norm_key);
CREATE TABLE IF NOT EXISTS company_enrichment (
    domain TEXT PRIMARY KEY, is_smb INTEGER, headcount TEXT, signal TEXT, fetched_at TEXT
);
"""


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").strip().lower())


def _key(company: str, role: str) -> str:
    return f"{_norm(company)}|{_norm(role)}"


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    return conn


def is_new(conn: sqlite3.Connection, job) -> bool:
    if conn.execute(
        "SELECT 1 FROM seen_jobs WHERE source=? AND raw_id=?",
        (job.source, job.raw_id),
    ).fetchone():
        return False
    if conn.execute(
        "SELECT 1 FROM seen_jobs WHERE norm_key=?", (_key(job.org, job.title),)
    ).fetchone():
        return False
    return True


def mark_seen(conn: sqlite3.Connection, job, score: int, status: str = "scored") -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs "
        "(source, raw_id, company, role, location, norm_key, first_seen, score, status) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (job.source, job.raw_id, job.org, job.title, job.location,
         _key(job.org, job.title),
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), score, status),
    )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cache.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cv_tailor/cache.py tests/test_cache.py
git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(cache): SQLite seen_jobs cross-source dedup"
```

---

## Task 2: Gate 1 — free pre-filter rules

**Files:**
- Create: `src/cv_tailor/gates.py`
- Test: `tests/test_gates.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gates.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.gates import is_remote, is_eu_eligible, has_target_keyword, passes_gate1
from cv_tailor.job_sources import JobPosting

KW = ["ai engineer", "python", "agentic", "automation"]


def _job(title, location, desc=""):
    return JobPosting(source="greenhouse", org="Acme", title=title, location=location,
                      url="https://x", description=desc, raw_id="1")


def test_remote_detection():
    assert is_remote("Remote - EMEA", "") is True
    assert is_remote("Berlin, Germany", "Fully remote within Europe") is True
    assert is_remote("New York (On-site)", "Onsite role") is False


def test_eu_eligibility():
    assert is_eu_eligible("Remote - Europe", "") is True
    assert is_eu_eligible("Remote - Global", "Work from anywhere") is True
    assert is_eu_eligible("Remote - US only", "Must be US-based") is False


def test_keyword_presence():
    assert has_target_keyword("Senior AI Engineer, Python", KW) is True
    assert has_target_keyword("Sales Development Rep", KW) is False


def test_passes_gate1_truth_table():
    good = _job("AI Engineer", "Remote - Europe", "Python, agentic systems")
    assert passes_gate1(good, KW) is True
    not_remote = _job("AI Engineer", "Berlin (On-site)", "Python")
    assert passes_gate1(not_remote, KW) is False
    wrong_geo = _job("AI Engineer", "Remote - US only", "Python")
    assert passes_gate1(wrong_geo, KW) is False
    wrong_role = _job("Account Executive", "Remote - Europe", "quota carrying")
    assert passes_gate1(wrong_role, KW) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_gates.py -v`
Expected: FAIL with `ModuleNotFoundError: cv_tailor.gates`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cv_tailor/gates.py
"""Gate 1: free rule-based pre-filter (remote / EU-eligible / target keyword).
Heuristic by design — the LLM scorer is the final arbiter. Goal is to cheaply
drop the obvious-no's before any paid enrichment or scoring call."""
from __future__ import annotations
import re

_REMOTE_RE = re.compile(r"\bremote\b|work from home|\bwfh\b|distributed|work from anywhere", re.I)
_ONSITE_RE = re.compile(r"on-?site|in-office|hybrid required", re.I)
_GLOBAL_RE = re.compile(r"\bglobal(ly)?\b|\bworldwide\b|\banywhere\b|\bemea\b", re.I)
_EU_RE = re.compile(
    r"\b(eu|europe|european|cet|cest|gmt|uk|united kingdom|ireland|germany|france|spain|"
    r"portugal|netherlands|belgium|poland|romania|bulgaria|italy|austria|switzerland|"
    r"sweden|norway|denmark|finland|estonia|lithuania|latvia|czech|greece|hungary)\b", re.I)
# US-only / non-EU exclusions that override a generic "remote".
_US_ONLY_RE = re.compile(r"us[- ]only|u\.s\.[- ]only|must be (us|united states)[- ]based|"
                         r"us work authorization|gc/?citizen", re.I)


def _blob(location: str, description: str, cap: int = 2000) -> str:
    return f"{location or ''} {(description or '')[:cap]}"


def is_remote(location: str, description: str) -> bool:
    text = _blob(location, description)
    if _REMOTE_RE.search(text):
        return True
    return False


def is_eu_eligible(location: str, description: str) -> bool:
    text = _blob(location, description)
    if _US_ONLY_RE.search(text):
        return False
    if _GLOBAL_RE.search(text):
        return True
    return bool(_EU_RE.search(text))


def has_target_keyword(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(k.lower() in low for k in keywords)


def passes_gate1(job, keywords: list[str]) -> bool:
    if not is_remote(job.location, job.description):
        return False
    if not is_eu_eligible(job.location, job.description):
        return False
    return has_target_keyword(f"{job.title} {job.description}", keywords)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_gates.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cv_tailor/gates.py tests/test_gates.py
git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(gates): Gate 1 free pre-filter rules"
```

---

## Task 3: Gate 2 — provenance SMB verdict

**Files:**
- Create: `src/cv_tailor/enrich.py`
- Test: `tests/test_enrich.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.enrich import is_smb, smb_hint
from cv_tailor.job_sources import JobPosting


def _job(source):
    return JobPosting(source=source, org="Acme", title="AI Engineer",
                      location="Remote - EU", url="https://x", description="", raw_id="1")


def test_startup_ats_is_smb():
    assert is_smb(_job("ashby")) is True
    assert is_smb(_job("greenhouse")) is True
    assert is_smb(_job("lever")) is True


def test_enterprise_hris_not_smb():
    assert is_smb(_job("workday")) is False
    assert is_smb(_job("successfactors")) is False


def test_smb_hint_string():
    assert "startup" in smb_hint(_job("greenhouse")).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_enrich.py -v`
Expected: FAIL with `ModuleNotFoundError: cv_tailor.enrich`

- [ ] **Step 3: Write minimal implementation**

```python
# src/cv_tailor/enrich.py
"""Gate 2: SMB (startup/scaleup) detection.

Phase 1 = provenance only: which board/ATS the posting came from is a strong,
free signal. Startup ATSs and remote-startup boards => SMB-likely; enterprise
HRIS => drop. Phase 2 adds a cached Hunter headcount lookup for ambiguous
aggregator (serpapi) hits."""
from __future__ import annotations

# Boards/ATS used overwhelmingly by startups & scaleups.
STARTUP_ATS = {"ashby", "greenhouse", "lever", "workable"}
# Remote-job boards: startup-skewed but mixed; provenance is weaker (Phase 2 refines).
REMOTE_BOARDS = {"remotive", "remoteok", "wwr", "himalayas"}
# Enterprise HRIS — almost never SMB.
ENTERPRISE_HRIS = {"workday", "successfactors", "taleo", "icims", "brassring", "smartrecruiters"}


def is_smb(job) -> bool:
    src = (job.source or "").lower()
    if src in ENTERPRISE_HRIS:
        return False
    if src in STARTUP_ATS or src in REMOTE_BOARDS:
        return True
    # Unknown provenance (e.g. serpapi in Phase 1) passes; Phase 2 Hunter refines.
    return True


def smb_hint(job) -> str:
    """A short company-size hint string to feed the LLM scorer."""
    src = (job.source or "").lower()
    if src in STARTUP_ATS:
        return "startup/scaleup (startup ATS)"
    if src in REMOTE_BOARDS:
        return "likely startup/scaleup (remote board)"
    if src in ENTERPRISE_HRIS:
        return "enterprise (enterprise HRIS)"
    return "unknown size"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_enrich.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/cv_tailor/enrich.py tests/test_enrich.py
git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(enrich): Gate 2 provenance SMB verdict"
```

---

## Task 4: Greenhouse + Lever connectors

**Files:**
- Modify: `src/cv_tailor/job_sources.py`
- Test: `tests/test_job_sources_v2.py`

- [ ] **Step 1: Write the failing test** (mocks `urllib.request.urlopen` so no network)

```python
# tests/test_job_sources_v2.py
import io, json, sys, pathlib
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources


def _fake_urlopen(payload):
    buf = io.BytesIO(json.dumps(payload).encode())
    cm = mock.MagicMock()
    cm.__enter__.return_value = buf
    return cm


def test_greenhouse_maps_fields():
    payload = {"jobs": [{"id": 42, "title": "AI Engineer",
                         "location": {"name": "Remote - EU"},
                         "absolute_url": "https://boards.greenhouse.io/acme/jobs/42",
                         "content": "&lt;p&gt;Build agents in Python&lt;/p&gt;"}]}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_greenhouse_org("acme", "Acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "greenhouse" and j.org == "Acme"
    assert j.title == "AI Engineer" and j.location == "Remote - EU"
    assert j.raw_id == "42"
    assert "Build agents in Python" in j.description  # entities + tags stripped


def test_lever_maps_fields():
    payload = [{"id": "abc", "text": "Backend Engineer",
                "categories": {"location": "Remote (Europe)", "commitment": "Full-time"},
                "hostedUrl": "https://jobs.lever.co/acme/abc",
                "descriptionPlain": "Python and TypeScript"}]
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_lever_org("acme", "Acme")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "lever" and j.title == "Backend Engineer"
    assert j.location == "Remote (Europe)" and j.raw_id == "abc"


def test_fetch_all_dispatches_new_kinds():
    payload_gh = {"jobs": []}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload_gh)):
        out = job_sources.fetch_all([{"kind": "greenhouse", "slug": "acme", "name": "Acme"}])
    assert out == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_job_sources_v2.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'fetch_greenhouse_org'`

- [ ] **Step 3: Write minimal implementation** — add to `src/cv_tailor/job_sources.py`

Add `import html` at the top (alongside the existing imports), then append these functions and extend `fetch_all`:

```python
def _http_json(url: str):
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "cv-tailor/0.2"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def fetch_greenhouse_org(org_slug: str, display_name: str | None = None) -> list[JobPosting]:
    """Greenhouse public board API. content is HTML-entity-encoded -> unescape then strip."""
    data = _http_json(f"https://boards-api.greenhouse.io/v1/boards/{org_slug}/jobs?content=true")
    name = display_name or org_slug
    out: list[JobPosting] = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") if isinstance(j.get("location"), dict) else ""
        out.append(JobPosting(
            source="greenhouse", org=name, title=j.get("title") or "",
            location=loc, url=j.get("absolute_url") or "",
            description=_strip_html(html.unescape(j.get("content") or "")),
            raw_id=str(j.get("id") or ""),
        ))
    return out


def fetch_lever_org(org_slug: str, display_name: str | None = None) -> list[JobPosting]:
    """Lever public postings API (returns a JSON list)."""
    data = _http_json(f"https://api.lever.co/v0/postings/{org_slug}?mode=json")
    name = display_name or org_slug
    out: list[JobPosting] = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        desc = j.get("descriptionPlain") or _strip_html(j.get("description") or "")
        out.append(JobPosting(
            source="lever", org=name, title=j.get("text") or "",
            location=cats.get("location") or "", url=j.get("hostedUrl") or "",
            description=desc, raw_id=j.get("id") or "",
        ))
    return out
```

Then extend `fetch_all` — replace its body's loop with:

```python
def fetch_all(sources: list[dict]) -> list[JobPosting]:
    """sources = [{'kind': 'ashby'|'greenhouse'|'lever', 'slug': '...', 'name': '...'}, ...]"""
    dispatch = {
        "ashby": fetch_ashby_org,
        "greenhouse": fetch_greenhouse_org,
        "lever": fetch_lever_org,
    }
    out: list[JobPosting] = []
    for s in sources:
        fn = dispatch.get(s["kind"])
        if not fn:
            print(f"warning: unknown source kind {s['kind']!r}; skipping")
            continue
        try:
            out.extend(fn(s["slug"], s.get("name")))
        except Exception as e:
            print(f"warning: fetch failed for {s.get('name', s['slug'])}: {e}")
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_job_sources_v2.py tests/ -v -k "source or ashby"`
Expected: PASS (new tests pass; existing ashby tests still pass)

- [ ] **Step 5: Commit**

```bash
git add src/cv_tailor/job_sources.py tests/test_job_sources_v2.py
git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(sources): greenhouse + lever connectors, dispatch table"
```

---

## Task 5: target_keywords in profile + sources additions

**Files:**
- Modify: `profile.yaml` (add a top-level block)
- Modify: `sources.yaml` (add greenhouse/lever examples)

- [ ] **Step 1: Add `target_keywords` to `profile.yaml`** (append at top level)

```yaml
# Used by Gate 1 (gates.py) to cheaply pre-filter before LLM scoring.
# Keep roughly in sync with the bias in match.py's scorer prompt.
target_keywords:
  - ai engineer
  - ai automation
  - agentic
  - llm
  - rag
  - python
  - typescript
  - forward deployed
  - solutions engineer
  - automation engineer
  - n8n
  - prompt
  - machine learning engineer
  - backend engineer
  - full stack
  - platform engineer
```

- [ ] **Step 2: Add example sources to `sources.yaml`** (append under `sources:`)

```yaml
  - kind: greenhouse
    slug: anthropic
    name: Anthropic
  - kind: lever
    slug: lever
    name: Lever
```

(These are placeholders to prove dispatch + the funnel; the real curated startup-source list is grown over time. Verify each slug resolves with a quick `curl` before relying on it.)

- [ ] **Step 3: Verify YAML parses**

Run: `python -c "import yaml; print(len(yaml.safe_load(open('sources.yaml'))['sources'])); print(yaml.safe_load(open('profile.yaml'))['target_keywords'][:3])"`
Expected: prints a count >= 7 and the first 3 keywords.

- [ ] **Step 4: Commit**

```bash
git add profile.yaml sources.yaml
git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(config): target_keywords + greenhouse/lever sources"
```

---

## Task 6: Funnel orchestration `scripts/scan.py` + quiet digest

**Files:**
- Create: `scripts/scan.py`
- Test: `tests/test_scan_funnel.py`

- [ ] **Step 1: Write the failing test** (funnel logic only — fetch + score injected as fakes)

```python
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
    keywords = ["ai engineer", "python"]
    jobs = [
        _job("greenhouse", "1", "AI Engineer", "Remote - EU", "Python"),        # passes
        _job("greenhouse", "2", "Account Executive", "Remote - EU", "sales"),   # fails gate1 (role)
        _job("greenhouse", "3", "AI Engineer", "Remote - US only", "Python"),   # fails gate1 (geo)
        _job("workday",    "4", "AI Engineer", "Remote - EU", "Python"),        # fails gate2 (enterprise)
    ]
    survivors = scan.run_gates(jobs, keywords, conn)
    assert [j.raw_id for j in survivors] == ["1"]

    # Mark #1 seen, re-run: now deduped out.
    from cv_tailor.cache import mark_seen
    mark_seen(conn, jobs[0], score=9)
    assert scan.run_gates(jobs, keywords, conn) == []


def test_quiet_digest_decides_send():
    assert scan.should_send([]) is False
    assert scan.should_send([{"score": 8}]) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_scan_funnel.py -v`
Expected: FAIL with `ModuleNotFoundError: scan` or `AttributeError: run_gates`

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/scan.py
#!/usr/bin/env python3
"""Daily job-discovery scanner (v2 funnel).

Pipeline: fetch -> Gate 1 (rules) -> Gate 2 (SMB provenance) -> Gate 3 (dedup vs
SQLite + CRM) -> LLM score survivors -> write digest -> quiet Telegram (only when
new qualifying roles exist). Scoring/tailoring/CRM unchanged.

Usage: python scripts/scan.py [--min-score 7] [--max-results 10] [--dry-run] [--no-dedupe]
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml

from cv_tailor.profile import load_profile
from cv_tailor.tailor_llm import build_azure_client
from cv_tailor.job_sources import fetch_all
from cv_tailor.match import score_job
from cv_tailor.digest import format_digest
from cv_tailor.telegram import format_digest_for_telegram, send_text
from cv_tailor.cache import connect, is_new, mark_seen
from cv_tailor.gates import passes_gate1
from cv_tailor.enrich import is_smb, smb_hint

DB_PATH = ROOT / "data" / "jobs.db"


def run_gates(jobs, keywords, conn):
    """Gate 1 (rules) -> Gate 2 (SMB) -> Gate 3 (dedup). Returns survivors."""
    survivors = []
    for j in jobs:
        if not passes_gate1(j, keywords):
            continue
        if not is_smb(j):
            continue
        if not is_new(conn, j):
            continue
        survivors.append(j)
    return survivors


def should_send(scored):
    return len(scored) > 0


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--min-score", type=int, default=7)
    p.add_argument("--max-results", type=int, default=10)
    p.add_argument("--dry-run", action="store_true", help="No Telegram, throwaway DB.")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = load_profile("profile.yaml")
    keywords = profile.get("target_keywords", [])
    with open(ROOT / "sources.yaml") as f:
        sources = yaml.safe_load(f)["sources"]

    db = ":memory:" if args.dry_run else DB_PATH
    conn = connect(db)

    print(f"fetching {len(sources)} sources...", file=sys.stderr)
    jobs = fetch_all(sources)
    print(f"  {len(jobs)} postings", file=sys.stderr)

    survivors = run_gates(jobs, keywords, conn)
    print(f"  {len(survivors)} passed gates (remote/EU/SMB/new)", file=sys.stderr)

    client = build_azure_client()
    scored = []
    for j in survivors:
        try:
            hint = smb_hint(j)
            r = score_job(profile, j.title, f"{j.location} [{hint}]", j.description, client=client)
            s = int(r.get("score", 0))
            mark_seen(conn, j, score=s)
            if s >= args.min_score:
                scored.append({"job": j, "score": s, "reason": r.get("reason", ""),
                               "keywords": r.get("key_keywords_matched", [])})
        except Exception as e:
            print(f"  score failed {j.org}/{j.title}: {e}", file=sys.stderr)

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_scan_funnel.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Full unit suite green**

Run: `python -m pytest -m "not integration" -q`
Expected: all pass (new + existing).

- [ ] **Step 6: Commit**

```bash
git add scripts/scan.py tests/test_scan_funnel.py
git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(scan): v2 funnel orchestration + quiet digest"
```

---

## Task 7: Dry-run end-to-end against live sources

**Files:** none (verification task)

- [ ] **Step 1: Run the funnel for real, no Telegram, throwaway DB**

Run: `cd ~/repos/cv-tailor && python scripts/scan.py --dry-run --min-score 7`
Expected: stderr shows fetch counts per stage; stdout prints the digest markdown. No Telegram send. Confirm: at least one Ashby/Greenhouse posting flows through; enterprise/non-EU/non-remote get dropped at the gates.

- [ ] **Step 2: Sanity-check the gates on real data**

If 0 survivors, loosen by inspecting: `python scripts/scan.py --dry-run --min-score 1` and read the stderr stage counts to see which gate is over-filtering; adjust `target_keywords` or the `_EU_RE`/`_REMOTE_RE` patterns and re-run. Commit any tuning.

- [ ] **Step 3: Commit any tuning**

```bash
git add -A && git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "chore(scan): gate tuning from live dry-run" || echo "no changes"
```

---

## Task 8: Daily launchd + health sentinel repoint

**Files:**
- Create: `scripts/run_scan.sh`
- Create: `~/Library/LaunchAgents/com.teodorlutoiu.cvtailor.daily.plist`
- Modify: `~/aios/health/registry.yaml` (cv-tailor entry)

- [ ] **Step 1: Create `scripts/run_scan.sh`**

```bash
#!/bin/bash
# Daily cv-tailor discovery scan. Invoked by com.teodorlutoiu.cvtailor.daily.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
mkdir -p scans/logs
TS=$(date +%Y-%m-%d)
PY="${PYTHON:-python3}"
"$PY" scripts/scan.py --min-score 7 >> "scans/logs/${TS}.log" 2>&1
```

- [ ] **Step 2: Create the daily plist** at `~/Library/LaunchAgents/com.teodorlutoiu.cvtailor.daily.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.teodorlutoiu.cvtailor.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/johnopenclaw/repos/cv-tailor/scripts/run_scan.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>WorkingDirectory</key><string>/Users/johnopenclaw/repos/cv-tailor</string>
  <key>StandardOutPath</key><string>/Users/johnopenclaw/repos/cv-tailor/scans/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/johnopenclaw/repos/cv-tailor/scans/logs/launchd.err.log</string>
</dict>
</plist>
```

- [ ] **Step 3: Bootstrap the daily job, bootout the weekly one**

```bash
UID_N=$(id -u)
launchctl bootout gui/$UID_N/com.teodorlutoiu.cvtailor.weekly 2>/dev/null || true
launchctl bootstrap gui/$UID_N ~/Library/LaunchAgents/com.teodorlutoiu.cvtailor.daily.plist
launchctl print gui/$UID_N/com.teodorlutoiu.cvtailor.daily | grep -iE 'state|program ='
```
Expected: job present, `state = ...` (not "running" continuously — it's calendar-scheduled). Leave the old weekly plist file in place but unloaded, or delete after confirming.

- [ ] **Step 4: Repoint the health sentinel** — edit `~/aios/health/registry.yaml`, change the `cv-tailor` entry's `label:` from `com.teodorlutoiu.cvtailor.weekly` to `com.teodorlutoiu.cvtailor.daily`. Verify:

```bash
cd ~/aios/health && ~/aios/.venv/bin/python cli.py check | grep -i "CV Tailor"
```
Expected: `✅ CV Tailor` (now tracking the daily label).

- [ ] **Step 5: Commit (cv-tailor repo)**

```bash
cd ~/repos/cv-tailor && git add scripts/run_scan.sh
git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(ops): daily run script (launchd entrypoint)"
```
(The plist lives outside the repo; the sentinel registry change is committed in the aios repo separately: `cd ~/aios && git add health/registry.yaml && git commit -m "health: repoint cv-tailor to daily label"`.)

---

## Deferred (follow-on plans at the phase checkpoints)

These are intentionally NOT in this plan — they get their own plan after the Phase 1 checkpoint (review of surfaced roles):

- **Phase 1b — remaining clean connectors:** `fetch_workable_org`, `fetch_remotive`, `fetch_remoteok`, `fetch_wwr` (RSS/XML), `fetch_himalayas`. Each follows the Task 4 connector recipe (`_http_json` + map to `JobPosting` + add to the `fetch_all` dispatch + a mocked-urlopen test). Confirm each board's live JSON/RSS shape with `curl` before mapping.
- **Phase 2 — SerpAPI + Hunter:** `fetch_serpapi(query)` Google-Jobs source (uses `SERPAPI_KEY`); upgrade `enrich.is_smb` to resolve company domain and do a cached Hunter headcount lookup (writes `company_enrichment`), using `HUNTER_API_KEY`. Both keys reused from icp-agent/ministeru; add to `.env` + `.env.example`.
- **Phase 3 — polish:** per-source health counts surfaced to the sentinel, dedup hardening, score-threshold tuning, optional `company_enrichment` TTL refresh.

---

## Self-Review

**Spec coverage:** Funnel (Gates 1-3) ✓ Task 2/3/6; SQLite dedup ✓ Task 1; Greenhouse/Lever connectors ✓ Task 4; target_keywords ✓ Task 5; quiet daily digest ✓ Task 6; launchd daily + sentinel repoint ✓ Task 8; SerpAPI/Hunter/remaining connectors ✓ explicitly deferred. Scoring/tailoring/CRM untouched ✓ (scan.py reuses `score_job`, `format_digest`, `send_text`; no edits to those modules).

**Placeholder scan:** No TBD/TODO. The two example sources in Task 5 are labeled placeholders to verify (correct practice for external slugs), not vague gaps.

**Type consistency:** `JobPosting` fields used everywhere are the real ones (`source`, `org`, `title`, `location`, `url`, `description`, `raw_id`). `score_job(profile, title, location, description, *, client)` called with keyword `client=` per its signature. `connect`/`is_new`/`mark_seen`/`passes_gate1`/`is_smb`/`smb_hint`/`run_gates`/`should_send` names match across tasks. `format_digest(scored, scan_date=...)` and `format_digest_for_telegram(scored, date_str)`/`send_text(text)` match `weekly_scan.py`'s existing usage.
