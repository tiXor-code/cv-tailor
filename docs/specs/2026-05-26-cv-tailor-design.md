# cv-tailor — Design Spec

- **Date**: 2026-05-26
- **Owner**: Teodor-Cristian Lutoiu
- **Status**: draft (awaiting owner review)
- **Repo**: `~/repos/cv-tailor/`

## Context

Teodor is exiting his Assistant Content Producer role at EA Bucharest and applying to new roles (primary target: AI automation / AI engineering positions; secondary: full-stack / founder-engineer roles). He wants:

1. A way to produce a **per-job tailored, ATS-safe PDF CV** from a single canonical profile.
2. A **lightweight CRM in Google Sheets** (in `contact@teodorlutoiu.com`'s Drive) to track every application end-to-end.

He drives both flows through Claude Code rather than typing commands himself, so the tooling is optimized to be invoked by an assistant, not by a human at a shell.

## Goals

- One canonical `profile.yaml` is the only source of truth for CV content.
- Each tailored CV is a single self-contained PDF, ATS-parseable, generated reproducibly from `(profile.yaml, jd.txt, fields.json)`.
- The Sheets CRM is created once and appended-to per application; status changes are driven through the assistant.
- Re-rendering a CV after editing `profile.yaml` or `fields.json` requires no LLM call.

## Non-goals

- No web UI, no scheduling, no n8n integration in this iteration.
- No automatic JD scraping from job-board URLs — the JD is provided as text or saved manually.
- No automated submission to ATS portals.
- No real-time recruiter parser scoring (Affinda / Sovren). A `pdftotext` sanity check is the only parse gate.
- No multi-language CVs in v1 (English only).
- No design flair (icons, columns, color blocks) — ATS-safety trumps aesthetics.

## Architecture

```
~/repos/cv-tailor/
├── profile.yaml                  # canonical profile (edited by Teodor)
├── .env                          # secrets (gitignored)
├── .secrets/sa.json              # Google service account key (gitignored)
├── templates/
│   ├── cv.html.j2                # Jinja2 CV layout
│   └── cv.css                    # print styles for WeasyPrint
├── scripts/
│   ├── tailor.py                 # JD path → tailored CV PDF + fields.json
│   ├── crm_setup.py              # one-time: headers, validation, formatting
│   └── crm_add.py                # fields.json → append row to Sheet
├── jobs/                         # one folder per job (gitignored)
│   └── <YYYY-MM-DD>-<company>-<role>/
│       ├── jd.txt
│       ├── fields.json
│       ├── cv.html
│       ├── cv.pdf
│       └── cv.txt                # pdftotext output, audit artifact
└── docs/specs/                   # this file lives here
```

**Stack:**
- Python 3.11+
- `click` not used (assistant invokes scripts directly with positional args)
- `pyyaml`, `jinja2`, `weasyprint`, `openai` (Azure mode), `gspread`, `google-auth`
- Azure OpenAI `gpt-4o-mini` for the single tailoring call
- WeasyPrint for HTML+CSS → PDF (no headless browser dependency)
- gspread + Google service account for Sheets writes

## profile.yaml — Schema

```yaml
contact:
  name: Teodor-Cristian Lutoiu
  email: contact@teodorlutoiu.com
  phone: "+40 725 697 859"
  location: Bucharest, Romania
  linkedin: linkedin.com/in/teodorlc
  github: github.com/tiXor-code

summary_pool:
  # Each is a paragraph the LLM can pick or rewrite from
  - id: ai_automation_engineer
    text: |
      AI automation engineer who ships end-to-end systems: LLM orchestration,
      RAG, retrieval pipelines, n8n workflows, and the production glue around
      Azure OpenAI, Claude, and embeddings. Built and shipped the Instantly
      support-agent stack (HMAC webhook → classify → embed → pgvector → draft
      → grade) and the icp-agent prospecting service on Vercel.
  - id: founder_entrepreneur
    text: |
      Founder of Ministeru' Creativ (4-person agency, live since 2026-03-18)
      and co-founder of JobMap. Builds revenue-generating systems end-to-end:
      product, infra, content, sales pipeline.
  - id: full_stack_engineer
    text: |
      Full-stack engineer (Python/Flask, Next.js/React, Postgres) with
      production deployments on Azure App Services and Vercel. Shipped
      JobMap.ro from zero to 9.3K live jobs and €59 paid product.
  - id: content_producer_pivot
    text: |
      Assistant Content Producer at EA Bucharest pivoting full-time to
      AI engineering. Combines games-industry production discipline with
      hands-on AI/automation shipping.

experiences:
  - id: ministeru
    role: Founder
    company: Ministeru' Creativ
    location: Bucharest, Romania (remote-first)
    dates: Mar 2026 – Present
    bullets:
      - Founded and run a 4-person creative agency; live since 2026-03-18 at ministerucreativ.com / .ro.
      - Built leads.ministerucreativ.com — internal AI prospecting dashboard with n8n side-pipeline (Q1 2026 experiment).
      - Lead client delivery: web, branding, content, automation.
      - Own all infrastructure: domain, hosting, CI/CD, analytics.
  - id: ea
    role: Assistant Content Producer
    company: Electronic Arts (Bucharest)
    location: Bucharest, Romania
    dates: <fill in start> – Present
    bullets:
      - <fill in concrete production bullets — shipped titles, cross-team coordination, etc.>
      - Coordinated content delivery across engineering, art, and design teams.
      - Drove production discipline (milestones, dependency tracking, risk surfacing).
  - id: jobmap
    role: Co-founder / Engineer
    company: JobMap (how-to-get-a-job.com)
    location: Remote
    dates: <fill in start> – Present
    bullets:
      - Shipped how-to-get-a-job.com end-to-end: Next.js frontend, Flask backend, Azure Postgres.
      - Live with 9.3K jobs, 504 skills, 247 admin tests, €59 paid product.
      - Built Council Step Pipeline (PR #5, merged 2026-03-09) — multi-agent admin workflow.
      - Hardened ingestion across 8+ job APIs (JSearch, Reed, Adzuna, etc.).
      - GEPA prompt optimization, security hardening, enrichment automation.

projects:
  - id: icp_agent
    name: ICP Agent
    tagline: Ideal-customer-profile prospecting service
    link: github.com/tiXor-code/icp-agent
    deployed: icp-agent-ten.vercel.app
    tech: [Hono, Vercel, Azure OpenAI, Hunter, SerpAPI, Google Sheets]
    bullets:
      - Built as Task A of an AI Automation Engineer take-home (Instantly).
      - One-shot prospecting agent: scrapes signals, scores ICP fit, returns enriched leads to Sheets.

  - id: instantly_support_agent
    name: Instantly Support Agent
    tagline: AI customer-support draft generator with self-grading verdict gate
    link: github.com/tiXor-code/instantly-support-agent
    deployed: instantly-support-agent.vercel.app
    tech: [n8n, Azure OpenAI (gpt-4o-mini, embeddings), Supabase pgvector, Claude CLI, HMAC]
    bullets:
      - End-to-end pipeline: HMAC webhook → n8n → classify → embed → pgvector RAG → Claude draft → self-grade → multi-signal verdict gate.
      - 136 tests across 5 layers including verdict-gate truth table extracted from the workflow JSON.
      - 12/12 eval agreement, 6/12 auto-rate at calibrated thresholds.

  - id: jobmap_platform
    name: JobMap (how-to-get-a-job.com)
    tagline: Job-search platform with multi-agent admin pipeline
    link: how-to-get-a-job.com
    tech: [Next.js, Flask, Azure Postgres, Azure App Services, GitHub Actions OIDC, Stripe]
    bullets:
      - 9.3K jobs, 504 skills, 247 admin tests, €59 corrective-protocol product live.
      - Council Step Pipeline shipped 2026-03-09 (PR #5).

  - id: orb_trading_bot
    name: ORB Trading Bot
    tagline: Opening-range-breakout bot on IBKR micro-futures
    tech: [Python, IBKR API, pandas, backtesting]
    bullets:
      - +$7,121 backtest P&L, 67.7% win rate on MNQ.
      - Identified robust signals via pattern study (5mo + year-long datasets), filtered out small-sample noise.

  - id: niche_sites
    name: honestcalculator.com
    tagline: AdSense content site, 15 US-finance calculators
    deployed: honestcalculator.com
    tech: [Next.js, Vercel, IndexNow, Google Search Console]
    bullets:
      - 15 cornerstone calculators (auto-loan-refi, coast-fire, SBA-loan, etc.) — combined TAM ~175K/mo.
      - Lighthouse desktop 94+/96+/100/100 across sampled pages.

  - id: portfolio
    name: teodorlutoiu.com
    tagline: AI-first WebGL editorial portfolio
    deployed: teodorlutoiu.com
    tech: [WebGL, Vite, Hostinger, Cloudflare]
    bullets:
      - Shipped redesign 2026-05-21 — root domain live with hashed assets, 200 OK across pages.

skills:
  languages: [Python, TypeScript, JavaScript, SQL]
  frameworks: [Next.js, React, Flask, FastAPI, Hono]
  ai:
    [
      Azure OpenAI (gpt-4o-mini, embeddings),
      Claude API,
      RAG / pgvector,
      agentic workflows,
      prompt engineering,
      eval design,
    ]
  data: [Postgres, Supabase, pgvector, Google Sheets, BigQuery (basic)]
  devops:
    [
      Azure App Services,
      Vercel,
      GitHub Actions (OIDC),
      n8n,
      Docker (basic),
      Cloudflare,
    ]
  tools: [Git, Figma, Stripe, Playwright]

education:
  - degree: BSc Computer Games Design and Development
    institution: University of Worcester
    year: 2022
    notes: Dissertation "AI in Games" (predates the LLM wave).

languages_spoken:
  - { language: Romanian, level: native }
  - { language: English, level: C2 / fluent }
```

Notes:
- `<fill in ...>` placeholders are explicit gaps Teodor must fill once during first review of `profile.yaml`; the tailoring script must refuse to run if any `<fill in` token survives.
- All bullets are pool items. The LLM picks subsets per JD; nothing is fabricated.

## Tailoring LLM Contract

**Single Azure OpenAI call (`gpt-4o-mini`, temperature 0.2, response_format JSON):**

System prompt encodes:
- The honesty guard: only pick existing IDs and bullet indices; do not invent.
- The output JSON schema below.
- "If profile is missing something the JD requires, list it under `gaps_honest`. Do not invent."
- "Rewrite of the summary is allowed (one short paragraph) but must only use facts present in profile.yaml."

User prompt: full `profile.yaml` (raw) + the JD text.

**Output JSON (`fields.json`) schema:**

```json
{
  "job_meta": {
    "company": "string",
    "role": "string",
    "location": "string | null",
    "jd_url": "string | null",
    "seniority_signal": "junior | mid | senior | lead | unspecified"
  },
  "chosen_summary_id": "string (id from summary_pool)",
  "summary_rewrite": "string (2-3 sentences, tailored, profile-grounded only)",
  "experience_ids_ordered": ["string", "..."],
  "experience_bullets": {
    "<experience_id>": [0, 2, 3]
  },
  "project_ids": ["string", "..."],
  "skills_emphasis": ["string", "..."],
  "jd_keywords_matched": ["string", "..."],
  "gaps_honest": ["string", "..."],
  "one_line_pitch": "string"
}
```

**Validation step (after the call, before rendering):**
- Every `experience_id` exists in `profile.yaml`.
- Every bullet index is within bounds.
- Every `project_id` exists.
- Every `skills_emphasis` item exists in `profile.yaml.skills.*`.
- If validation fails → save the raw response to `fields.invalid.json` for debugging and exit with a clear error.

## Render Pipeline (`scripts/tailor.py`)

```
1. Parse arg: <jd-path> (absolute or relative to cwd)
2. Read jd text; derive default slug from --slug arg, else from LLM-extracted job_meta
   (company + role kebab-cased, prefixed with YYYY-MM-DD).
3. Create jobs/<slug>/ ; copy jd to jobs/<slug>/jd.txt
4. Load profile.yaml; assert no "<fill in" tokens remain
5. Azure OpenAI call → validate → save fields.json
6. Jinja2 render templates/cv.html.j2 with (profile, fields) → cv.html
7. WeasyPrint cv.html + templates/cv.css → cv.pdf
8. pdftotext -layout cv.pdf cv.txt
9. ATS sanity checks on cv.txt:
   - 5 headings present in order: Summary, Experience, Projects, Skills, Education
   - email regex hits exactly once in first 3 lines
   - phone regex hits exactly once in first 3 lines
   - no spaced-letter mangling (`T e o d o r`)
   - chosen experience/project IDs appear by company/project name
10. Print: paths, one_line_pitch, gaps_honest, any sanity warnings
```

If any sanity check fails, the script exits non-zero but keeps all artifacts for inspection.

## Template (`cv.html.j2`) — ATS Hard Rules

- Semantic HTML: one `<section>` per CV section, `<h2>` for section heading, `<h3>` for role/project name.
- Section headings exact strings: `Summary`, `Experience`, `Projects`, `Skills`, `Education`.
- No `<table>` used for layout. No `<img>`. No `<svg>` for text. No icon fonts.
- Bullets render as `<ul><li>`.
- Dates as `Mar 2026 – Present` (en-dash, three-letter month).
- Contact line as first text block on page 1: `Name | Location · email · phone · linkedin · github`.
- Hyperlinks present as visible text AND `<a href>`.

## CSS (`cv.css`)

- Font stack: `Inter, Arial, Helvetica, sans-serif` (Inter embedded by WeasyPrint).
- Body 10.5pt, secondary 9pt, headings 12pt bold.
- Color: very dark grey `#1a1a1a` body, mid grey `#666` for dates/locations. No accent color.
- Single column, full width.
- `@page { size: A4; margin: 18mm; }`. No `@top-*` or `@bottom-*` regions.
- Bold for company names, project names, and emphasized skills only.
- No CSS grid for content layout (used only minimally for the contact line if needed).

## Output Paths

```
~/repos/cv-tailor/jobs/2026-05-26-instantly-ai-automation-engineer/
  jd.txt
  fields.json
  cv.html        # debug
  cv.pdf         # the artifact to upload
  cv.txt         # ATS-eye-view audit
```

Slug rule: `<YYYY-MM-DD>-<company-kebab>-<role-kebab>`. Date = generation date, not application date (application date lives in the CRM).

## CRM — Google Sheets

**Sheet:** `Job Applications - CRM` in contact@teodorlutoiu.com's Drive.
**Tab:** `Pipeline`.

| Col | Field | Type / Validation |
|-----|-------|--------------------|
| A | Company | text |
| B | Role | text |
| C | Location | text |
| D | JD link | hyperlink |
| E | CV file | local absolute path, rendered as `file://...` link |
| F | Date applied | date (blank until applied) |
| G | Status | dropdown: `Saved`, `Applied`, `Reply`, `Interview`, `Offer`, `Rejected`, `Ghosted` |
| H | Next action | text |
| I | Notes | text |

**Formatting (set once by `crm_setup.py`):**
- Frozen header row, bold headers, header background light grey.
- Auto-filter on row 1.
- Data validation on column G (list of statuses, reject invalid input).
- Conditional formatting per status:
  - `Saved` → background `#f0f0f0`
  - `Applied` → background `#cfe2ff`
  - `Reply` → background `#fff3cd`
  - `Interview` → background `#ffe0b2`
  - `Offer` → background `#c8e6c9`
  - `Rejected` → background `#f5c6cb`, italic
  - `Ghosted` → background `#f5c6cb`, strikethrough
- Column widths tuned (Company narrow, Notes wide).
- Default sort: column F desc, then column G alpha.

## Access — Service Account, One-Time Setup

```
1. Create GCP project "cv-tailor" (free tier).
2. Enable Google Sheets API and Google Drive API on the project.
3. Create service account "cv-tailor-bot" → generate JSON key →
   save to ~/repos/cv-tailor/.secrets/sa.json (gitignored).
4. Drive MCP attempts to create the empty Sheet
   "Job Applications - CRM" in contact@teodorlutoiu.com's Drive.
   - If the Drive MCP is auth'd to a different account, stop and fall back:
     hand Teodor an .xlsx to import manually into the right account.
5. Teodor shares that Sheet with the service account's email (Editor role).
6. Sheet ID + SA path → .env.
7. Run `python scripts/crm_setup.py` to apply headers, validation, formatting.
```

After step 7, every later write goes via gspread (no further clicks).

## `crm_add.py` Behavior

Input: a path to `fields.json` (the script reads the same folder for `cv.pdf`).

Append one row:
- Company, Role, Location, JD link → from `fields.json.job_meta`
- CV file → absolute path to `cv.pdf`
- Date applied → blank
- Status → `Saved`
- Next action → `Apply`
- Notes → blank

Idempotency: if any existing row has the same Company + Role (case-insensitive match), the script prints a warning and exits non-zero rather than duplicating. Override with a `--force` flag (argparse) if needed.

## Status Updates (No New Tooling)

When Teodor tells the assistant "I applied to X" / "Got a reply from Y" / etc., the assistant updates the matching row via gspread directly (using a small helper imported by `crm_add.py`). No standalone update script.

## End-to-End Flow

```
[Teodor pastes JD + says "process this"]
                  │
                  ▼
[Assistant saves jd.txt to jobs/<slug>/]
                  │
                  ▼
[python scripts/tailor.py jobs/<slug>/jd.txt]
       → fields.json, cv.html, cv.pdf, cv.txt
                  │
                  ▼
[Assistant reports cv.pdf path + gaps_honest]
                  │
                  ▼
[python scripts/crm_add.py jobs/<slug>/fields.json]
       → row appended in Sheet
                  │
                  ▼
[Teodor uploads cv.pdf to the job's ATS portal]
                  │
                  ▼
[Teodor tells assistant "applied"]
       → assistant updates Status=Applied, Date applied=today
```

## Risks & Open Questions

- **WeasyPrint install on macOS** can need system libs (`brew install pango cairo gdk-pixbuf libffi`). Setup script should detect and guide.
- **Inter availability** — if not installed system-wide, WeasyPrint will fall back to Arial. Acceptable for ATS, but visually different. Optional follow-up: bundle Inter `.woff2` and reference via `@font-face`.
- **EA role bullets** are marked `<fill in>` — Teodor must populate before first run.
- **JD parsing for company/role** is LLM-extracted; if a JD is messy and the LLM picks the wrong name, the slug + CRM row are wrong. Mitigation: a `--slug` and `--company` / `--role` override arg on `tailor.py`.
- **Drive MCP write capability** is uncertain at the cell level; the design routes all cell writes through gspread to sidestep this.

## Out of Scope (future)

- Cover letter generator (would slot in as a second LLM call + second template).
- JD scraping from a URL.
- Slack / email notifications on status change.
- Multi-language CV variants.
- Real ATS-parser scoring loop (Affinda / Sovren).
