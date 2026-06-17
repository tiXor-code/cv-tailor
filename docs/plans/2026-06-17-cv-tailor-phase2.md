# CV Tailor v2 Discovery — Phase 2 Implementation Plan (SerpAPI + Hunter)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add Google Jobs (via SerpAPI) as an aggregator source for real "all-European-markets" recall, and upgrade the SMB gate with a cached Hunter headcount lookup so aggregator results are sharply filtered to startups/scaleups.

**Architecture:** A new `fetch_serpapi` connector feeds `source="serpapi"` postings into the existing funnel. Gate 2 (`enrich.is_smb`) becomes connection-aware: known ATS/board sources keep provenance (Phase 1); `serpapi`/unknown sources resolve a company domain and do a cached Hunter `companies/find` lookup, classifying SMB by employee range. All Hunter verdicts cache in the existing `company_enrichment` SQLite table.

**Tech Stack:** Python 3.11+, stdlib urllib/sqlite3, Azure OpenAI (existing), SerpAPI, Hunter.io, pytest.

**Keys:** `SERPAPI_API_KEY` (64-char) + `HUNTER_API_KEY` (40-char) are already in `.env` (pulled from the icp-agent Vercel project). Code reads those exact names.

**API shapes (confirmed):**
- SerpAPI: `GET https://serpapi.com/search?engine=google_jobs&q=<query>&location=<loc>&hl=en&ltype=1&api_key=<k>` → `{"jobs_results":[{title, company_name, location, via, share_link, description, job_id, detected_extensions:{work_from_home,...}, apply_options:[{title, link}]}], "serpapi_pagination":{"next_page_token":...}}`. `ltype=1` = remote only.
- Hunter: `GET https://api.hunter.io/v2/companies/find?domain=<d>&api_key=<k>` → `{"data":{"metrics":{"employees":"11-50"}, ...}}` (range string; may be null).

**Spec:** `docs/specs/2026-06-17-cv-tailor-discovery-design.md` (Phase 2 bullet).

---

## File Structure

| File | Change |
|---|---|
| `src/cv_tailor/cache.py` | add `get_enrichment` / `put_enrichment` (company_enrichment table accessors, TTL-aware) |
| `src/cv_tailor/enrich.py` | add `company_domain`, `classify_headcount`, `hunter_headcount`; make `is_smb(job, conn=None)` + `smb_hint(job, conn=None)` connection-aware |
| `src/cv_tailor/job_sources.py` | add `fetch_serpapi`; handle `kind: serpapi` in `fetch_all` (query/location config) |
| `scripts/scan.py` | `run_gates` calls `is_smb(j, conn)`; scoring loop calls `smb_hint(j, conn)` |
| `sources.yaml` | add a few `kind: serpapi` query entries |
| `.env.example` | add `SERPAPI_API_KEY=` + `HUNTER_API_KEY=` |
| tests | `test_enrich_hunter.py`, `test_serpapi.py`, extend `test_cache.py` |

Backward-compat: `is_smb(job)` / `smb_hint(job)` with no `conn` keep Phase-1 provenance behavior, so existing `tests/test_enrich.py` stays green.

---

## Task P1: company_enrichment cache accessors

**Files:** Modify `src/cv_tailor/cache.py`; extend `tests/test_cache.py`.

- [ ] **Step 1: Add failing tests** (append to `tests/test_cache.py`)

```python
def test_enrichment_roundtrip(tmp_path):
    from cv_tailor.cache import connect, get_enrichment, put_enrichment
    conn = connect(tmp_path / "jobs.db")
    assert get_enrichment(conn, "acme.com") is None
    put_enrichment(conn, "acme.com", is_smb=True, headcount="11-50", signal="hunter")
    row = get_enrichment(conn, "acme.com")
    assert row["is_smb"] is True and row["headcount"] == "11-50" and row["signal"] == "hunter"


def test_enrichment_ttl(tmp_path):
    from cv_tailor.cache import connect, get_enrichment, put_enrichment
    conn = connect(tmp_path / "jobs.db")
    put_enrichment(conn, "old.com", is_smb=False, headcount="5001-10000", signal="hunter")
    # max_age_days=0 means anything is stale -> treated as a miss
    assert get_enrichment(conn, "old.com", max_age_days=0) is None
    assert get_enrichment(conn, "old.com") is not None  # default window: hit
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_cache.py -q` → FAIL (no get_enrichment).

- [ ] **Step 3: Implement** — append to `src/cv_tailor/cache.py`:

```python
def put_enrichment(conn, domain, is_smb, headcount, signal):
    conn.execute(
        "INSERT OR REPLACE INTO company_enrichment "
        "(domain, is_smb, headcount, signal, fetched_at) VALUES (?,?,?,?,?)",
        (domain.lower(), 1 if is_smb else 0, headcount, signal,
         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.commit()


def get_enrichment(conn, domain, max_age_days=30):
    row = conn.execute(
        "SELECT is_smb, headcount, signal, fetched_at FROM company_enrichment WHERE domain=?",
        (domain.lower(),),
    ).fetchone()
    if not row:
        return None
    try:
        fetched = datetime.strptime(row[3], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    age_days = (datetime.now(timezone.utc) - fetched).total_seconds() / 86400
    if age_days > max_age_days:
        return None
    return {"is_smb": bool(row[0]), "headcount": row[1], "signal": row[2], "fetched_at": row[3]}
```

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_cache.py -q` → PASS.
- [ ] **Step 5: Commit** `git add src/cv_tailor/cache.py tests/test_cache.py && git -c user.name=tiXor-code -c user.email=tiXor-code@users.noreply.github.com commit -m "feat(cache): company_enrichment accessors with TTL"`

---

## Task P2: domain extraction + headcount classification

**Files:** Modify `src/cv_tailor/enrich.py`; Create `tests/test_enrich_hunter.py`.

- [ ] **Step 1: Failing test** `tests/test_enrich_hunter.py`:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor.enrich import company_domain, classify_headcount
from cv_tailor.job_sources import JobPosting


def _job(url, source="serpapi"):
    return JobPosting(source=source, org="Acme", title="AI Engineer", location="Remote - EU",
                      url=url, description="", raw_id="1")


def test_company_domain_skips_job_boards():
    assert company_domain(_job("https://acme.com/careers/ai-eng")) == "acme.com"
    assert company_domain(_job("https://boards.greenhouse.io/acme/jobs/1")) is None
    assert company_domain(_job("https://www.linkedin.com/jobs/view/123")) is None
    assert company_domain(_job("")) is None


def test_classify_headcount():
    assert classify_headcount("1-10") is True
    assert classify_headcount("201-500") is True
    assert classify_headcount("501-1000") is False
    assert classify_headcount("10001+") is False
    assert classify_headcount(None) is None
    assert classify_headcount("garbage") is None
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_enrich_hunter.py -q` → FAIL.

- [ ] **Step 3: Implement** — add to `src/cv_tailor/enrich.py` (add `import re` and `from urllib.parse import urlparse` at top):

```python
JOB_BOARD_DOMAINS = {
    "greenhouse.io", "boards.greenhouse.io", "lever.co", "jobs.lever.co",
    "ashbyhq.com", "jobs.ashbyhq.com", "workable.com", "linkedin.com",
    "indeed.com", "glassdoor.com", "google.com", "ziprecruiter.com",
    "remotive.com", "remoteok.com", "weworkremotely.com", "himalayas.app",
    "wellfound.com", "builtin.com", "smartrecruiters.com",
}
SMB_EMPLOYEE_CEILING = 500  # startups & scaleups up to ~500


def _registrable(host: str) -> str:
    host = (host or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def company_domain(job):
    """Best-effort company domain from the posting URL; None for job-board URLs."""
    host = _registrable(urlparse(job.url or "").netloc)
    if not host:
        return None
    if host in JOB_BOARD_DOMAINS or any(host.endswith("." + b) for b in JOB_BOARD_DOMAINS):
        return None
    return host


def classify_headcount(employees):
    """Hunter range string -> is_smb (True/False), or None if unknown/unparseable."""
    if not employees:
        return None
    m = re.search(r"(\d+)\s*\+?\s*$", employees.replace(",", ""))
    upper = re.findall(r"\d+", employees.replace(",", ""))
    if not upper:
        return None
    top = int(upper[-1])  # use the upper bound of the range ("201-500" -> 500)
    return top <= SMB_EMPLOYEE_CEILING
```

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_enrich_hunter.py -q` → PASS.
- [ ] **Step 5: Commit** `git add src/cv_tailor/enrich.py tests/test_enrich_hunter.py && git -c ... commit -m "feat(enrich): company domain extraction + headcount classification"`

---

## Task P3: Hunter lookup + connection-aware is_smb / smb_hint

**Files:** Modify `src/cv_tailor/enrich.py`; extend `tests/test_enrich_hunter.py`.

- [ ] **Step 1: Failing tests** (append to `tests/test_enrich_hunter.py`):

```python
def test_is_smb_uses_hunter_for_serpapi(tmp_path, monkeypatch):
    import cv_tailor.enrich as enrich
    from cv_tailor.cache import connect, get_enrichment
    conn = connect(tmp_path / "jobs.db")
    calls = {"n": 0}
    def fake_hunter(domain, api_key=None):
        calls["n"] += 1
        return "11-50" if domain == "acme.com" else "5001-10000"
    monkeypatch.setattr(enrich, "hunter_headcount", fake_hunter)

    smb_job = _job("https://acme.com/careers/1")
    big_job = _job("https://megacorp.com/jobs/1")
    assert enrich.is_smb(smb_job, conn) is True
    assert enrich.is_smb(big_job, conn) is False
    # verdict cached -> second call doesn't re-hit hunter
    assert enrich.is_smb(smb_job, conn) is True
    assert calls["n"] == 2  # one per distinct domain only


def test_is_smb_provenance_unchanged_without_conn():
    assert _job("x", source="greenhouse") and __import__("cv_tailor.enrich", fromlist=["is_smb"]).is_smb(_job("x", source="greenhouse")) is True
    from cv_tailor.enrich import is_smb
    assert is_smb(_job("x", source="workday")) is False
```

- [ ] **Step 2:** Run → FAIL (no hunter_headcount; is_smb ignores conn).

- [ ] **Step 3: Implement** — add `hunter_headcount` and rewrite `is_smb`/`smb_hint` in `src/cv_tailor/enrich.py` (add `import json, os` and `import urllib.request` at top):

```python
def hunter_headcount(domain, api_key=None):
    """Return Hunter's employee-range string for a domain, or None on any failure."""
    api_key = api_key or os.environ.get("HUNTER_API_KEY")
    if not api_key:
        return None
    url = f"https://api.hunter.io/v2/companies/find?domain={domain}&api_key={api_key}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "cv-tailor/0.2"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        return ((data or {}).get("data") or {}).get("metrics", {}).get("employees")
    except Exception:
        return None


def _hunter_verdict(job, conn):
    """SMB verdict for an ambiguous (serpapi/unknown) source via cached Hunter lookup.
    Returns True/False, or None if undeterminable (caller falls back to pass)."""
    domain = company_domain(job)
    if not domain:
        return None
    cached = get_enrichment(conn, domain)
    if cached is not None:
        return cached["is_smb"]
    headcount = hunter_headcount(domain)
    verdict = classify_headcount(headcount)
    if verdict is None:
        return None
    put_enrichment(conn, domain, is_smb=verdict, headcount=headcount, signal="hunter")
    return verdict


def is_smb(job, conn=None):
    src = (job.source or "").lower()
    if src in ENTERPRISE_HRIS:
        return False
    if src in STARTUP_ATS or src in REMOTE_BOARDS:
        return True
    # Ambiguous (serpapi / unknown): use Hunter when a cache+conn is available.
    if conn is not None:
        v = _hunter_verdict(job, conn)
        if v is not None:
            return v
    return True  # undeterminable -> pass; the LLM scorer is the final arbiter


def smb_hint(job, conn=None):
    src = (job.source or "").lower()
    if src in STARTUP_ATS:
        return "startup/scaleup (startup ATS)"
    if src in REMOTE_BOARDS:
        return "likely startup/scaleup (remote board)"
    if src in ENTERPRISE_HRIS:
        return "enterprise (enterprise HRIS)"
    if conn is not None:
        domain = company_domain(job)
        if domain:
            cached = get_enrichment(conn, domain)
            if cached and cached.get("headcount"):
                return f"~{cached['headcount']} employees (Hunter)"
    return "unknown size"
```
Requires `get_enrichment`, `put_enrichment` imported from cache at top of enrich.py: `from cv_tailor.cache import get_enrichment, put_enrichment`.

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_enrich_hunter.py tests/test_enrich.py -q` → PASS (new + Phase-1 enrich tests).
- [ ] **Step 5: Commit** as `feat(enrich): cached Hunter SMB verdict for aggregator sources`

---

## Task P4: SerpAPI connector

**Files:** Modify `src/cv_tailor/job_sources.py`; Create `tests/test_serpapi.py`.

- [ ] **Step 1: Failing test** `tests/test_serpapi.py` (mock urlopen):

```python
import io, json, sys, pathlib
from unittest import mock
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from cv_tailor import job_sources


def _fake_urlopen(payload):
    buf = io.BytesIO(json.dumps(payload).encode())
    cm = mock.MagicMock(); cm.__enter__.return_value = buf
    return cm


def test_serpapi_maps_fields():
    payload = {"jobs_results": [{
        "title": "AI Engineer", "company_name": "Acme", "location": "Remote - Europe",
        "via": "via LinkedIn", "share_link": "https://serpapi.com/x", "description": "Python agents",
        "job_id": "ID123",
        "detected_extensions": {"work_from_home": True},
        "apply_options": [{"title": "LinkedIn", "link": "https://www.linkedin.com/jobs/1"},
                          {"title": "Acme", "link": "https://acme.com/careers/1"}]}]}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        jobs = job_sources.fetch_serpapi("AI engineer", location="Europe", api_key="k")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "serpapi" and j.org == "Acme" and j.title == "AI Engineer"
    assert j.raw_id == "ID123"
    # url prefers the non-job-board apply link (company careers page) for Hunter domain extraction
    assert j.url == "https://acme.com/careers/1"


def test_fetch_all_dispatches_serpapi():
    payload = {"jobs_results": []}
    with mock.patch("urllib.request.urlopen", return_value=_fake_urlopen(payload)):
        out = job_sources.fetch_all([{"kind": "serpapi", "query": "AI engineer", "location": "Europe"}])
    assert out == []
```

- [ ] **Step 2:** Run → FAIL (no fetch_serpapi).

- [ ] **Step 3: Implement** — add to `src/cv_tailor/job_sources.py` (add `import os` and `from urllib.parse import urlparse, quote_plus` at top):

```python
_SERP_JOB_BOARDS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
                    "google.com", "serpapi.com")


def _best_company_url(apply_options, share_link):
    """Prefer an apply link that is NOT a big job board (likelier a company domain),
    so Hunter can resolve a real company domain later. Fall back to share_link."""
    for opt in apply_options or []:
        link = (opt or {}).get("link") or ""
        host = urlparse(link).netloc.lower()
        if link and not any(b in host for b in _SERP_JOB_BOARDS):
            return link
    if apply_options and apply_options[0].get("link"):
        return apply_options[0]["link"]
    return share_link or ""


def fetch_serpapi(query: str, location: str | None = None, api_key: str | None = None,
                  hl: str = "en") -> list[JobPosting]:
    """Google Jobs via SerpAPI. Remote-only (ltype=1). One page (~10 results)."""
    api_key = api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        print("warning: SERPAPI_API_KEY not set; skipping serpapi source")
        return []
    params = [f"engine=google_jobs", f"q={quote_plus(query)}", f"hl={hl}", "ltype=1",
              f"api_key={api_key}"]
    if location:
        params.append(f"location={quote_plus(location)}")
    data = _http_json("https://serpapi.com/search?" + "&".join(params))
    out: list[JobPosting] = []
    for j in data.get("jobs_results", []):
        out.append(JobPosting(
            source="serpapi", org=j.get("company_name") or "",
            title=j.get("title") or "", location=j.get("location") or "",
            url=_best_company_url(j.get("apply_options"), j.get("share_link")),
            description=j.get("description") or "", raw_id=j.get("job_id") or "",
        ))
    return out
```
Then extend `fetch_all`: serpapi entries are query-based, not slug-based, so add a special branch BEFORE the dispatch-table lookup:

```python
    for s in sources:
        if s["kind"] == "serpapi":
            try:
                out.extend(fetch_serpapi(s["query"], s.get("location")))
            except Exception as e:
                print(f"warning: serpapi fetch failed for {s.get('query')!r}: {e}")
            continue
        fn = dispatch.get(s["kind"])
        ...
```

- [ ] **Step 4:** Run `.venv/bin/python -m pytest tests/test_serpapi.py tests/test_job_sources.py tests/test_job_sources_v2.py -q` → PASS (new + existing connector tests).
- [ ] **Step 5: Commit** `feat(sources): SerpAPI Google Jobs connector`

---

## Task P5: wire scan.py + sources.yaml + .env.example

**Files:** Modify `scripts/scan.py`, `sources.yaml`, `.env.example`.

- [ ] **Step 1:** In `scripts/scan.py`, make Gate 2 cache-aware: in `run_gates`, change `if not is_smb(j):` to `if not is_smb(j, conn):`; in the scoring loop change `hint = smb_hint(j)` to `hint = smb_hint(j, conn)`. (Existing `test_scan_funnel.py` uses provenance sources with a real conn, so it still passes — greenhouse/workday short-circuit before any Hunter call.)

- [ ] **Step 2:** Append SerpAPI queries to `sources.yaml`:

```yaml
  - kind: serpapi
    query: "AI engineer remote europe"
    location: "Europe"
  - kind: serpapi
    query: "AI automation engineer remote"
    location: "Europe"
  - kind: serpapi
    query: "forward deployed engineer remote europe"
    location: "Europe"
```

- [ ] **Step 3:** Append to `.env.example`:

```
SERPAPI_API_KEY=
HUNTER_API_KEY=
```

- [ ] **Step 4:** Run the FULL unit suite `.venv/bin/python -m pytest -m "not integration" -q` → all pass.
- [ ] **Step 5: Commit** `feat(scan): enable SerpAPI source + Hunter-backed Gate 2`

---

## Task P6: Live integration verification (real keys)

**Files:** none.

- [ ] **Step 1:** Dry-run end to end (keys in `.env`): `cd ~/repos/cv-tailor && set -a; source .env; set +a; .venv/bin/python scripts/scan.py --dry-run --min-score 7`. Confirm stderr shows postings climbing (SerpAPI adds to the ~478) and a non-zero "passed gates" count; confirm a `company_enrichment` cache builds (inspect with `.venv/bin/python -c "import sqlite3; ..."` on a non-dry persistent run, or add a temp print).
- [ ] **Step 2:** Confirm Hunter is actually consulted: run a tiny script that builds a `serpapi` JobPosting with a known small-company careers URL and calls `enrich.is_smb(job, conn)` with real key → expect a real verdict + a cached row.
- [ ] **Step 3:** Sanity: ensure SerpAPI query count is modest (3 queries/day) to bound cost; note SerpAPI free tier ~100 searches/mo, so 3/day ≈ 90/mo — adjust if needed.
- [ ] **Step 4:** No commit (verification only); note results for the user.

---

## Self-Review

**Spec coverage:** SerpAPI Google Jobs source ✓ P4; cached Hunter SMB enrichment writing `company_enrichment` ✓ P1+P3; domain resolution ✓ P2; keys reused from icp-agent + `.env.example` ✓ P5; wired into the funnel ✓ P5. Remaining connectors (Phase 1b) and polish (Phase 3) still deferred.

**Placeholder scan:** none — every step has complete code.

**Type consistency:** `is_smb(job, conn=None)` / `smb_hint(job, conn=None)` signatures match all callers (scan.run_gates passes conn; Phase-1 tests pass no conn). `get_enrichment`/`put_enrichment` names consistent across cache + enrich. `company_domain`/`classify_headcount`/`hunter_headcount`/`_hunter_verdict`/`_best_company_url`/`fetch_serpapi` referenced consistently. `JobPosting` fields (source/org/title/location/url/description/raw_id) used correctly throughout.
