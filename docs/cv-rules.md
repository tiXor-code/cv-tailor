# CV generation rules

Version 1.1, 2026-10-08. These rules govern every CV that cv-tailor builds for Teodor: the
per-job tailored CVs and the general LinkedIn Easy Apply CV. Rule IDs are stable, so the
generator prompt, the linter and reviews can cite them. Teodor approved the recommendations
on 2026-10-08, and the rules are enforced in code (section 8).

## TLDR

Every CV tells one story: an AI automation and solutions builder who ships tools people
use, proven with results. Page 1 carries a target-role headline, a three-sentence summary
with numbers, and the agency work written as result-first blocks. Every bullet says what
changed, for whom, and how it was checked, using only facts from `profile.yaml`. The format
stays plain on purpose: one column, standard headings, PDF plus DOCX, two pages, no photo.

## Why these rules exist

On 2026-10-08 the general CV had 16 bullets and only 2 stated a result (51 of 55 AI-search
citations; 30% shorter project timelines). The other bullets described features, listed
tech, or used filler ("significantly enhancing", "facilitated seamless"). The summary
opened with a 2022 dissertation, and the strongest work sat in a Projects section under
internal product names. Teodor did not like the framing.

These rules come from three research passes on 2026-10-08 (results-focused writing; ATS
parsing and format; framing for AI builders, career changers and founders), with the key
sources spot-checked, and from a reference CV Teodor admires (Appendix A). Sources are in
Appendix C.

## 1. Positioning

- **P1. One story per CV.** He is an AI automation and solutions builder. Game and content
  production appear as evidence of delivery, scale and stakeholder work, never as a second
  identity.
- **P2. Headline under the name.** It names the target role family in the employer's words.
  - General CV: `AI Automation & Solutions Specialist | Agentic workflows, RAG, AI search (GEO)`.
  - Tailored CV: the posting's own title when it describes work he has done.
  - The headline is a target, never a claimed past title.
- **P3. Real titles on every role, identical to LinkedIn.** No CEO. No "Engineer" title he
  did not hold, because those invite coding screens for work he does not do by hand. The
  agency title is "Co-Founder & AI Automation Lead" (decided 2026-10-08): a specialist title
  that matches recruiter searches for his target roles. LinkedIn must carry the same title.
- **P4. Summary: at most 3 sentences and 70 words, no pronouns.**
  - Sentence 1: the role label and how he works (ships with Claude Code).
  - Sentence 2: one or two proof points with numbers.
  - Sentence 3: scope, or the kind of role he wants.
  - Never open with education or the dissertation, and never use claims nobody can check
    ("proven track record", "passionate").
- **P5. Builder framing.** Name Claude Code once, as the method, and claim what he owns: the
  spec, the review, the testing and the deployment.
  - Never write "vibe coding".
  - Never claim hand-written code or software-engineering years.
  - Never use apologetic labels such as "Builds with (via AI tools)".
- **P6. Concurrent roles are separate entries** with real, overlapping dates. The agency entry
  says what the company is in one line: size, founding month, what it sells.
- **P7. Specialist over executive.** Founder bullets describe hands-on work (built, shipped,
  measured), not running a company. In a field experiment, former founders got 43% fewer
  callbacks than non-founders, driven by older hiring firms and worst for successful
  founders. AI startups, by contrast, hire founders readily.

## 2. Results

- **R1. Bullet formula: verb, result, how.** Put the result first whenever there is one
  (Google's XYZ formula). For AI work, answer four questions:
  1. Who uses it?
  2. How often?
  3. What changed?
  4. How is it checked?
- **R2. Every agency bullet carries at least one concrete proof:**
  - a number with a baseline;
  - a scale, volume or frequency;
  - adoption (who uses it, how many);
  - or a live link.

  Across the whole CV, at least 70% of bullets contain a result number. Dates and test
  counts do not count.
- **R3. Numbers come with an anchor:** "from X to Y", "X of Y", a percentage plus the
  absolute figure, or a time period. A bare number has no scale.
- **R4. Use a proxy when there is no business metric.** Proxies are:
  - scale (pages, clients, users);
  - volume or frequency (per week);
  - time saved;
  - a before and after state;
  - adoption.

  With none of those, state the purpose and who relies on it.
- **R5. Engineering internals only explain how something is checked.** This covers test
  counts, eval agreement and thresholds. Translate them for a non-engineer ("agreed with the
  expected verdict on 12 of 12 evaluation cases") and never use them as the headline result.
- **R6. "Production" and "live" only for systems real people use now.** Demos and
  experiments are called that, or cut.
- **R7. Strongest and most relevant first.**
  - Recent or relevant roles get 3-5 bullets; older or unrelated roles get 1-2.
  - Space follows relevance, not tenure.
- **R8. Bullets are 12-30 words, at most two lines.** Bold one result phrase per project
  block, nothing else.

## 3. Honesty

- **H1. `profile.yaml` is the only source of facts.** A new fact enters the profile first,
  with a source and Teodor's confirmation. The generator never adds facts, numbers,
  clients, tools or titles.
- **H2. Numbers are copied exactly.** Estimates carry "~" or a range. No false precision, no
  rounding up.
- **H3. Every line must survive "tell me more" in an interview.**
- **H4. No keyword stuffing, hidden or white text, or prompt-injection text.** Recruiters
  treat these as deception, and some reject on sight.
- **H5. Client names, contract values and anything from EA** appear only when Teodor has
  approved them for public use.

## 4. Language

- **L1. No "I", "my" or "me".** Present tense for current roles, past tense for past roles.
- **L2. Banned words and phrases.** The linter fails the CV on any hit.
  - Duty and helper phrases: responsible for, duties included, helped, assisted,
    contributed to, involved in, various.
  - Self-praise: results-driven, proven track record, passionate, dynamic, team player,
    detail-oriented, self-motivated, hard worker, go-getter, think outside the box,
    synergy, thought leader, value add, best of breed.
  - Filler adverbs: significantly, successfully, seamlessly, effectively, efficiently.
  - Words that mark text as AI-written: leverage(d), utilize(d), spearheaded, robust,
    seamless, pivotal, showcasing, delve, realm, intricate, underscore, cutting-edge,
    game-changer, innovative.
- **L3. No em dashes.** Use commas, colons and full stops instead. Date ranges use an en
  dash or a hyphen.
- **L4. Plain verbs:** built, shipped, launched, cut, grew, automated, replaced, trained, ran,
  led, designed, measured, sold.
- **L5. Internal product names become plain descriptions of what they do.** This covers
  SGEO, ICP Agent, AIOS, SEO Sentinel, Agent HQ, Anvil and Aegis. A public product with a
  live URL may keep its name next to the link. This extends Teodor's 2026-09-29 rule for
  outreach; he confirmed it for CVs on 2026-10-08.
- **L6. Write each acronym out once, then abbreviate:** "retrieval-augmented generation (RAG)".
  ATS keyword filters match exact terms, so both forms should appear.
- **L7. One spelling variant per CV** (UK or US), matching the posting.

## 5. Structure and format

- **S1. One column.**
  - No tables, text boxes, sidebars, icons, skill bars, photos or letter-spacing tricks.
  - Contact details go in the body, not in a Word header or footer.
- **S2. Section order:**
  1. Header (name, headline, contact, links)
  2. Summary
  3. Experience (newest first)
  4. Selected projects (optional, at most 2)
  5. Skills
  6. Education
  7. Languages

  Standard headings only.
- **S3. The agency role holds the AI work as 3-4 blocks** (the reference-CV pattern).
  - Each block is a bold heading naming what was built (no internal product name), then
    1-2 bullets ending in the result.
  - Work done for or at the agency always lives here.
  - Personal tools with real users go to Selected projects; experiments are cut.
- **S4. Role line:** real title, company, city and country with work mode (remote, hybrid,
  on-site), and dates as `Mon YYYY – Mon YYYY` with English month names. Never numeric
  dates: 03.2026 reads differently in Romania and the US.
- **S5. Header line:**
  `Bucharest, Romania (UTC+2/+3) | Remote | phone | email | linkedin.com/in/teodorlc | github.com/tiXor-code | portfolio`.
  - Links are printed as visible URLs and are also clickable.
  - Add "EU citizen" only for postings in the EU or EEA.
- **S6. No personal details beyond city and country.** No photo, date of birth, age, marital
  status, gender, nationality line, CNP or street address. A photo version exists only if a
  Romanian or German-speaking employer explicitly asks.
- **S7. Skills: at most 5 grouped lines** of hard skills the target role uses, in the
  posting's words.
  - No soft-skill lists, no proficiency bars.
  - Key skills also appear inside bullets, because parsers date a skill by where it is used.
- **S8. Languages section, always:** English C2, Romanian native, Spanish elementary.
- **S9. Length: at most 2 A4 pages.**
  - Page 1 must stand alone, with the headline, the summary and the three strongest results.
  - Body text 10-11 pt, margins 1.3-2 cm.
- **S10. Files.**
  - Text-based PDF by default (LinkedIn Easy Apply, Greenhouse, Ashby, Lever).
  - DOCX for Workable, or when an autofill preview comes back garbled.
  - Keep files under 300 KB; LinkedIn recommends under 2 MB.
  - Names: `Teodor-Lutoiu-CV.pdf`, or `Teodor-Lutoiu-CV-<Company>.pdf` when tailored.
  - PDF title and author metadata are set.

## 6. Tailoring per job

- **T1. What changes per posting:** only the headline, the summary, the order of blocks and
  bullets, and which skills are emphasised. Facts never change.
- **T2. Use the posting's exact terms for requirements he truly meets.** ATS keyword filters
  and recruiter Boolean searches need exact matches. AI ranking (Greenhouse Talent
  Matching, Workday HiredScore, Ashby) looks for evidence, so tie each keyword to a result.
- **T3. Requirements he does not meet go to `gaps_honest`** for Teodor, never into the CV.
- **T4. Cover at least 70% of the posting's terms that the profile supports.** Never add
  unsupported terms.
- **T5. Location filters out more applications than layout does.**
  - Prefer postings open to Romania, the EU or EMEA.
  - Keep LinkedIn's Skills section in sync with the CV skills, because LinkedIn's matching
    reads both.

## 7. Quality gates for every generated CV

| ID | Check | Where |
|----|-------|-------|
| Q1 | ATS simulation scores 100 for PDF and DOCX (one column, contact, sections, roles, characters, metadata) | `ats_sim.py` (exists) |
| Q2 | At most 2 pages | `variants.pdf_page_count` (exists) |
| Q3 | Zero hits from the L2 list, zero em dashes | `cv_rules.lint_cv_text` (report); `tests/test_profile_rules.py` (profile) |
| Q4 | At least 70% of bullets carry a result number that is not a year | `cv_rules.lint_cv_text` (warning) |
| Q5 | Summary: at most 3 sentences and 70 words, no pronouns, does not open with education | `cv_rules.apply_cv_rules` (replaces a breaking summary) |
| Q6 | No internal product names | `cv_rules` (lint and profile test) |
| Q7 | Every number in the CV appears in `profile.yaml` | `cv_rules.apply_cv_rules` checks the generated headline and summary; bullets are copied from the profile |
| Q8 | Role titles match LinkedIn | `linkedin_sync` (exists) |
| Q9 | Spell check passes | to add |

## 8. How the rules are enforced

- `profile.yaml` is held to the rules by `tests/test_profile_rules.py`. Every summary,
  headline, bullet, description, project name and tagline must pass, and every `emphasis`
  phrase must appear in a bullet. The generator copies bullets verbatim, so this test is
  the main guarantee.
- The headline and summary are the only text an LLM writes. `cv_rules.apply_cv_rules` runs
  after every tailoring call (`assemble.py`, the ATS second pass, `scripts/tailor.py`) and
  replaces either one with the profile's own text when it breaks a rule. It also drops
  projects marked `status: demo` and projects already shown under a listed role
  (`covered_by`).
- `cv_rules.lint_cv_text` measures each rendered CV. The report goes to `meta.json`
  (`cv_rules`) and the CLI, and it never blocks a send.
- The templates render the headline, the agency description line, one bold result phrase
  per bullet (`emphasis`), and Certifications and Languages sections. The `languages` skill
  row is labelled "Stack".
- The ATS simulator counts study years listed under Education as covered time, and tells
  two roles at one employer apart by title.

## Appendix A. Lessons from the reference CV

Teodor shared an AI engineer's CV (Canva, January 2026) as inspiration. It is used for
framing only, never for facts.

**Taken:**

- A target title under the name, plus a one-line specialisation.
- Each role as a container of named project blocks. Every block covers what was built, its
  scale, and a result.
- Results with baselines:
  - time before and after;
  - volume per week;
  - accuracy as a percentage;
  - effort avoided against the original estimate.
- The result line in bold, so a skimming reader finds it.
- Domain context (regulated industries, client sectors) and the work mode on every role.
- Languages and Awards sections.

**Left out:**

- The two-column layout and the photo. When its text was extracted, the sidebar (contact,
  education, skills) interleaved with the experience column. That is the parse failure ATS
  vendors document (S1).
- Dense paragraphs. These rules use 1-2 line bullets (R8).
- Unprovable phrasing such as "Expert in" and "Proven track record" (L2).
- Mixed date formats and typos.

## Appendix B. Current lines rewritten under these rules

These use only facts already in `profile.yaml`. Gaps are marked `[?]` and need Teodor's
input before they ship.

1. **AI-search agent**
   - Before: "Built SGEO, an autonomous SEO and AI-search (GEO) agent: tracks Google
     rankings and whether ChatGPT, Gemini and Perplexity cite a site, then proposes and
     ships fixes [...] One live site is cited by an AI assistant for 51 of 55 target searches."
   - After: "Got a live site cited by an AI assistant for **51 of 55 target searches** with a
     self-built AI-search agent that tracks ChatGPT, Gemini and Perplexity citations and
     ships fixes." Still needed: the baseline before the agent, and over what period `[?]`.
2. **Game production**
   - Before: "Transitioned from Game Designer to Producer, successfully managing project
     milestones and launch." and "Streamlined development processes through Agile
     methodologies, achieving a 30% reduction in project timelines."
   - After: "Promoted from game designer to producer and led the game to launch; **cut
     project timelines by 30%** by moving the team to Agile sprints."
3. **Support drafting agent**
   - Before: "12/12 eval agreement, 6/12 auto-rate at calibrated thresholds." and "136 tests
     across 5 layers [...]".
   - After: "Built a customer-support drafting agent (n8n, RAG, Claude) with a self-grading
     quality gate; it **agreed with the expected verdict on 12 of 12 evaluation cases**,
     guarded by 136 automated tests."
4. **EA**
   - Before: "Developed and executed content strategies, significantly enhancing player
     engagement and retention."
   - After: "Plan and deliver `[?]` content releases per season for EA FC Ultimate Team, a live
     service with millions of players, coordinating `[?]` teams; `[result?]`." This needs
     Teodor's numbers, and only what EA allows to be shared.

## Appendix C. Sources (checked 2026-10-08)

Results-focused writing:
- Laszlo Bock, XYZ formula: https://www.linkedin.com/pulse/20140929001534-24454816-my-personal-formula-for-a-better-resume
- Harvard: https://careerservices.fas.harvard.edu/resources/create-a-strong-resume/
- MIT: https://capd.mit.edu/resources/career-toolkit-crafting-an-effective-resume/
- Stanford: https://careered.stanford.edu/sites/g/files/sbiybj22801/files/media/file/developing_your_resume_handout.pdf
- Yale: https://ocs.yale.edu/resources/writing-impactful-resume-bullets/
- Duke, summary statements: https://careerhub.students.duke.edu/summary-statements/
- Prospects (UK): https://www.prospects.ac.uk/careers-advice/cvs-and-cover-letters/how-to-write-a-cv/
- Gayle Laakmann McDowell: https://www.dice.com/career-advice/how-to-make-your-resume-bullets-jump
- Ladders eye-tracking, 2018: https://www.hrdive.com/news/eye-tracking-study-shows-recruiters-look-at-resumes-for-7-seconds/541582/
- CareerBuilder deal-breakers, 2018: https://www.prnewswire.com/news-releases/employers-share-their-most-outrageous-resume-mistakes-and-instant-deal-breakers-in-a-new-careerbuilder-study-300701888.html
- Greenhouse AI trust survey, 2025: https://www.greenhouse.com/newsroom/an-ai-trust-crisis-70-of-hiring-managers-trust-ai-to-make-faster-and-better-hiring-decisions-only-8-of-job-seekers-call-it-fair
- Vocabulary of LLM-written text: https://arxiv.org/abs/2404.01268 and https://arxiv.org/abs/2406.07016

ATS and format:
- Greenhouse, failed parses (tables, headers, footers, contact in header): https://support.greenhouse.io/hc/en-us/articles/200989175-Unsuccessful-resume-parse
- Greenhouse, auto-reject only on yes/no and pick-list questions: https://support.greenhouse.io/hc/en-us/articles/360000653472-Auto-reject
- Greenhouse keyword filtering: https://support.greenhouse.io/hc/en-us/articles/27104809835291-Talent-Filtering
- Workable, how an ATS reads resumes: https://resources.workable.com/stories-and-insights/how-ATS-reads-resumes
- Textkernel, column layouts: https://www.textkernel.com/learn-support/blog/improving-extraction-from-column-resumes/
- Lever, resume parsing: https://help.lever.co/s/article/Understanding-Resume-Parsing
- LinkedIn Easy Apply (Word or PDF, under 2 MB): https://www.linkedin.com/help/linkedin/answer/a510363
- Ashby AI-assisted review: https://docs.ashbyhq.com/ai-assisted-application-review
- Workday HiredScore grades: https://doc.workday.com/hiredscore/en-us/workday-hiredscore/recruiter-productivity-/reference--candidate-grades.html
- Where the "75% rejected by ATS" myth comes from: https://phys.org/news/2026-07-wrong-modern-job.html
- Hidden-text tricks in resumes (NYT, 2025): https://www.nytimes.com/2025/10/07/business/ai-chatbot-prompts-resumes.html
- UK National Careers Service, personal details: https://nationalcareers.service.gov.uk/careers-advice/cv-sections
- EEOC, photographs: https://www.eeoc.gov/prohibited-employment-policiespractices
- Romanian Labour Code art. 29: https://www.codulmuncii.ro/titlul_2/capitolul_1/art_29_1.html
- LinkedIn skills match: https://www.linkedin.com/help/recruiter/answer/a593591

Framing for AI builders and founders:
- Cursor, Forward Deployed Strategist ("you don't need to be the person writing the production code"): https://jobs.ashbyhq.com/cursor/6960dfef-6dc1-4dbb-887b-0fc9e6e4292e
- OpenAI, forward deployed engineer posting: https://jobs.ashbyhq.com/openai/05223b5a-5ff1-4fe7-9f97-4ef7fc68b8ef
- Anthropic, candidate AI guidance ("create your first draft yourself"): https://www.anthropic.com/candidate-ai-guidance
- Zapier, AI fluency bar in hiring ("repeatable systems, not one-off prompts"): https://zapier.com/blog/raising-ai-fluency-bar-in-hiring/
- Simon Willison on vibe coding: https://simonwillison.net/2025/Mar/19/vibe-coding/
- Stack Overflow Developer Survey 2025, AI: https://survey.stackoverflow.co/2025/ai
- Botelho and Chang, founder penalty field experiment (Organization Science, 2023): https://ideas.repec.org/a/inm/ororsc/v34y2023i1p484-508.html
- Ramp, forward deployed engineering: https://builders.ramp.com/post/forward-deployed-engineering
- LinkedIn Recruiter title search: https://www.linkedin.com/help/recruiter/answer/a415295

Evidence notes:
- The "6-7 seconds per CV" figure is a first-pass sorting time from a small industry
  study, not a reading time.
- There is no credible data on whether matching a job title exactly, or using a founder
  title versus a functional one, changes callbacks. P2, P3 and P7 rest partly on inference.
- Surveys from resume-tool companies are treated as weak evidence throughout.
