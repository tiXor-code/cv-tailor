# CV Tailor v2 — European remote-SMB discovery engine

**Status:** design approved (brainstorming, 2026-06-17) · **Author:** Teodor (with Claude) · **Supersedes discovery layer of:** `docs/specs/2026-05-26-cv-tailor-design.md`

## Context

CV Tailor today scans **5 hardcoded Ashby boards** weekly, LLM-scores each job
against `profile.yaml`, dedupes against a Google Sheet, and Telegrams a digest;
on approval it tailors a PDF CV and logs to a Sheets CRM. The discovery layer is
too narrow for the real need: surfacing the **European remote startup/scaleup
market** for Teodor's AI-engineering profile as he exits EA. The scoring +
CV-tailoring + CRM machinery is solid and stays; we rebuild only the **discovery
half** into a wide-net, hard-filtered, cost-bounded funnel.

## Goals

- Broad coverage of European remote roles at **startups & scaleups** (stage/energy, ~<250, VC-backed).
- Eligibility = **widest**: any company that can employ in the EU *or* take an EU contractor (Teodor invoices via his PFA), including global-remote (US/UK).
- Surface **high-fit, apply-worthy** roles (reuse the existing LLM scorer).
- Keep LLM cost roughly flat despite ~10x more sources, via a cost-ordered funnel.

## Non-goals

- No change to `profile.yaml`, `tailor_llm.py`/`render.py`/`ats_check.py`, the Sheets CRM, or the approve→PDF flow.
- Single-user (Teodor). No productization.
- No ToS-violating scraping of LinkedIn/Indeed — use Google Jobs via SerpAPI instead.

## Decisions (locked in brainstorming)

| Decision | Choice |
|---|---|
| Primary goal | Coverage + apply-worthy volume + sharp SMB targeting (combined) |
| "SMB" means | Startups & scaleups (stage/energy, ~<250) |
| Eligibility | Widest — anyone who hires EU (employee or PFA contractor), incl. global-remote |
| Sourcing | Hybrid — clean ATS/remote APIs + Google Jobs via SerpAPI |
| SMB detection | ATS-provenance signal + cached Hunter headcount lookup |
| Cadence/delivery | Daily scan; Telegram digest only when new roles clear the score threshold |

## Architecture — cost-ordered funnel

```
        ┌─ ATS APIs: Ashby, Greenhouse, Lever, Workable (public job JSON) ─┐
SOURCES ┼─ Remote boards: Remotive, RemoteOK, WeWorkRemotely, Himalayas   ─┼→ raw JobPosting[]
        └─ Google Jobs via SerpAPI (EU-remote × role-keyword queries)     ─┘
                                   │ normalize → JobPosting{company,role,location,url,desc,source,raw_id}
   GATE 1  free rules: remote? EU-eligible-or-contractor? role keywords?      (regex/rules; drops ~70-90%)
   GATE 2  SMB gate: ATS provenance + cached Hunter headcount; drop enterprise (cheap, cached)
   GATE 3  dedup vs SQLite (seen across all sources + CRM)                     (no re-scoring)
   SCORE   existing LLM scorer (match.py), fed a company-size hint            (Azure, ~cents each, survivors only)
   DELIVER digest.py + telegram.py → approve rows → existing tailor (UNCHANGED)
```

Cost order is the core idea: free rules first, cheap cached enrichment second,
the expensive LLM call last and only on survivors — so LLM volume stays ~flat
even as source breadth grows ~10x.

## Components — new vs. reused (paths under repo root)

| Component | Status | Notes |
|---|---|---|
| `src/cv_tailor/job_sources.py` | **extend** | `fetch_all(sources)` already dispatches on `kind` returning the generic `JobPosting` dataclass; add `greenhouse`/`lever`/`workable`/`remotive`/`remoteok`/`wwr`/`himalayas`/`serpapi` fetchers behind the same interface |
| `sources.yaml` | **extend** | richer multi-`kind` config; SerpAPI query permutations (role keywords × EU-remote) live here |
| `src/cv_tailor/gates.py` | **new** | Gate 1 free rules: remote detection, EU-eligible/contractor heuristic, role-keyword presence |
| `src/cv_tailor/enrich.py` | **new** | Gate 2 SMB detection: ATS-provenance map (Ashby/Lever/Greenhouse/Workable ⇒ startup; Workday/SuccessFactors/Taleo/iCIMS ⇒ enterprise) + Hunter headcount fallback, cached |
| `src/cv_tailor/cache.py` + `data/jobs.db` | **new** | SQLite: `seen_jobs`, `company_enrichment`; cross-source dedup + enrichment cache |
| `src/cv_tailor/match.py` | **reuse** | existing scorer; pass the company-size hint from `enrich.py` into the prompt |
| `src/cv_tailor/digest.py`, `telegram.py` | **reuse** | same digest + chunked delivery; add a "quiet" (only-when-new) gate |
| `scripts/weekly_scan.py` → `scripts/scan.py` | **rename/extend** | orchestrates the funnel; daily, idempotent |
| `scripts/run_weekly_scan.sh` → `scripts/run_scan.sh` | **rename** | launchd entrypoint |
| `tailor_llm.py`, `scripts/tailor.py`, `render.py`, `ats_check.py`, `sheets.py`, `scripts/process_approved.py` | **untouched** | approve→PDF→Sheets unchanged |

## Target keyword set (Gate 1)

Today the role-fit logic is implicit in `match.py`'s scorer prompt. Gate 1 needs an
**explicit** keyword set (e.g. AI engineer, AI automation, agentic, Python, TypeScript,
forward-deployed, solutions engineer, RAG, LLM, n8n…) to cheaply pre-filter before the
LLM. Source it from a new `target_keywords` block in `profile.yaml` (or `sources.yaml`),
kept in sync with the scorer's bias.

## SQLite schema (`data/jobs.db`)

- `seen_jobs(source TEXT, raw_id TEXT, company TEXT, role TEXT, location TEXT, first_seen TEXT, score INT, status TEXT, PRIMARY KEY(source, raw_id))` — dedup across all sources + scan history; a role seen via SerpAPI and via the company's Greenhouse board is scored once.
- `company_enrichment(domain TEXT PRIMARY KEY, is_smb INT, headcount TEXT, signal TEXT, fetched_at TEXT)` — caches provenance/Hunter verdicts so no company is paid for twice; TTL refresh (~30d).

Google Sheets stays the human-facing CRM; SQLite is machine dedup/cache only.

## Gates in detail

- **Gate 1 (free):** drop if not remote, not EU-eligible (location not EU/EMEA and not "global/anywhere remote" and no contractor signal), or no target keyword present. Pure rules, no API calls.
- **Gate 2 (SMB, cached):** primary signal = which ATS/board the posting came from (startup ATS ⇒ likely SMB; enterprise HRIS ⇒ drop). For ambiguous aggregator (SerpAPI) hits, resolve the company domain and do a cached **Hunter** company lookup for headcount; classify startup/scaleup vs enterprise. Cache every verdict.
- **Gate 3 (dedup):** normalize `(company, role)` + check `seen_jobs` and the Sheets CRM; only unseen roles proceed to scoring.

## Cadence & delivery

- launchd `com.teodorlutoiu.cvtailor.weekly` → **daily** (new label `com.teodorlutoiu.cvtailor.daily`, ~09:00). Update the **health sentinel** registry entry `cv-tailor` in `~/aios/health/registry.yaml` to the new label so the morning brief tracks it.
- Daily scan writes all survivors to cache; Telegram a digest **only when ≥1 new role clears the score threshold** (configurable, e.g. `--min-score 7`). Silent on dry days.

## Build phasing (checkpoint-gated)

- **Phase 1** — funnel skeleton + SQLite cache + Gate 1 + clean ATS/remote-board connectors (Greenhouse/Lever/Workable/Remotive/RemoteOK/WWR/Himalayas) + provenance-only Gate 2 + daily quiet digest, reusing the scorer. Proves the pipeline end-to-end on free sources. **Checkpoint: review surfaced roles.**
- **Phase 2** — add SerpAPI aggregator source + Hunter enrichment in Gate 2 (with cache). **Checkpoint: coverage/precision check.**
- **Phase 3** — polish: per-source health signal (feed the sentinel), dedup hardening, threshold tuning.

## Error handling

- Per-source failures swallowed (one dead board never kills a scan) — existing pattern, extended; each source logs a count + status; a source returning 0 when it usually returns >0 is flagged.
- Scan is **idempotent**: re-running the same day re-reads cache, never double-scores or double-delivers.
- Missing API keys (SerpAPI/Hunter/Telegram) degrade gracefully (skip that source/step, log it), never crash the scan.

## Secrets

Add `SERPAPI_KEY` + `HUNTER_API_KEY` to the cv-tailor `.env` (reuse the keys already
used by icp-agent / ministeru; confirm quota at build time). `.env.example` updated to match.

## Verification

- **Unit (`pytest -m "not integration"`):** each new connector with mocked HTTP fixtures returning a `JobPosting`; `gates.py` pure-function tests (remote/eligible/keyword truth table); `enrich.py` provenance map + Hunter-mock + cache-hit tests; `cache.py` dedup against a temp DB; scorer size-hint passthrough.
- **Integration (`-m integration`, real keys):** one live fetch per source kind; one live SerpAPI EU-remote query; one live Hunter lookup — assert non-empty + schema.
- **End-to-end dry run:** `python scripts/scan.py --dry-run --min-score 7` runs the full funnel against real sources, prints the digest, writes a throwaway DB, sends nothing. Confirm Gate 2 drops a known enterprise and keeps a known startup; confirm dedup skips a role already in `seen_jobs`.
- **Delivery:** one real digest send to Telegram on a day with ≥1 new qualifying role (or forced).
- **Idempotency:** run the scan twice same-day → second run scores 0 new, sends nothing.
- **Regression:** existing approve→PDF→Sheets flow still works (`scripts/process_approved.py` on a sample scan); existing test suite green.
