"""Interview prep pack: likely questions, STAR stories, probes, gaps, money.

Inputs: profile.yaml, the archived JD (descriptions.json), the cover letter
that went out, answers.yaml floors. The model proposes; this module checks
every story against the profile before anything is written:

  - a story must cite a real profile source id (experience/project/education);
  - every number in a story or defence must appear in that source's own text.

A story that fails either check is dropped and its question is moved to the
honest-gaps list, so the pack never puts an invented fact in his mouth. The
salary rehearsal is arithmetic over answers.yaml (offer_eval.rehearsal), never
the model's.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

from cv_tailor.offer_eval import rehearsal
from cv_tailor.scout_queue import _validated_day, queue_root, read_description
from cv_tailor.slug import job_slug

MAX_QUESTIONS = 15
MAX_PROBES = 5
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

SYSTEM_PROMPT = """You prepare a candidate for a job interview. You get his facts file
(profile, with ids), the job posting, and the cover letter he sent.

Return ONLY a JSON object:
{
  "questions": [{"question": str, "why": str, "source_id": str|null,
                 "situation": str, "task": str, "action": str, "result": str}],
  "probes": [{"claim": str, "source_id": str, "defence": str}],
  "gaps": [{"topic": str, "note": str}]
}

Rules:
- questions: the 15 questions this interviewer is most likely to ask, driven by what
  the posting emphasises. "why" names the posting requirement behind it.
- For each question pick the ONE profile item (by its id) whose facts answer it and write
  a STAR answer using ONLY facts and numbers present in that item. Never add a number,
  employer, tool, team size or outcome that the item does not state. If no item answers
  the question, set source_id to null and leave situation/task/action/result empty.
- probes: the 5 claims on his CV an interviewer is most likely to push on (quote the
  claim from the profile), with a one-line defence grounded in the same item.
- gaps: requirements in the posting the profile cannot answer. Say so plainly and suggest
  how to answer honestly. Do not soften a gap into a strength.
- Plain English, no em dashes."""


def profile_sources(profile: dict) -> dict:
    """source id -> {"label", "text"} where text is everything the profile says
    about that item (the only facts a story may use)."""
    out = {}
    for kind in ("experiences", "projects"):
        for item in profile.get(kind) or []:
            sid = item.get("id")
            if not sid:
                continue
            label = (f"{item.get('role')} at {item.get('company')}" if kind == "experiences"
                     else item.get("name") or sid)
            out[sid] = {"label": label, "text": json.dumps(item, ensure_ascii=False)}
    for i, ed in enumerate(profile.get("education") or []):
        out[f"education_{i}"] = {"label": f"{ed.get('degree')}, {ed.get('institution')}",
                                 "text": json.dumps(ed, ensure_ascii=False)}
    return out


def _numbers(text: str) -> set:
    return {n.replace(",", "") for n in _NUM_RE.findall(text or "")}


def ungrounded_numbers(text: str, source_text: str) -> list:
    have = _numbers(source_text)
    return sorted(n for n in _numbers(text) if n not in have)


def build_messages(profile: dict, jd: str, cover_letter: str, company: str, role: str) -> list:
    facts = {
        "summaries": [s.get("text") for s in profile.get("summary_pool") or []],
        "items": {sid: json.loads(src["text"]) for sid, src in profile_sources(profile).items()},
        "skills": profile.get("skills"),
    }
    user = (f"ROLE: {role} at {company}\n\nPROFILE:\n{json.dumps(facts, ensure_ascii=False)}\n\n"
            f"POSTING:\n{(jd or '(posting text not archived)')[:8000]}\n\n"
            f"COVER LETTER SENT:\n{(cover_letter or '(none)')[:3000]}")
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def call_llm(messages: list, *, client) -> dict:
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
    resp = client.chat.completions.create(model=deployment, messages=messages,
                                          temperature=0.2,
                                          response_format={"type": "json_object"})
    data = json.loads(resp.choices[0].message.content)
    if not isinstance(data, dict):
        raise ValueError("model returned a non-object")
    return data


def validate(raw: dict, profile: dict) -> dict:
    """Keep only grounded stories/defences; demote the rest to gaps.
    Returns {"questions", "probes", "gaps", "dropped"}."""
    sources = profile_sources(profile)
    questions, probes, dropped = [], [], []
    gaps = [{"topic": str(g.get("topic") or "").strip(), "note": str(g.get("note") or "").strip()}
            for g in raw.get("gaps") or [] if isinstance(g, dict) and g.get("topic")]
    for q in (raw.get("questions") or [])[:MAX_QUESTIONS]:
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            continue
        item = {"question": str(q["question"]).strip(), "why": str(q.get("why") or "").strip(),
                "source_id": None, "star": None}
        sid = q.get("source_id")
        star = {k: str(q.get(k) or "").strip() for k in ("situation", "task", "action", "result")}
        if sid:
            if sid not in sources:
                dropped.append({"question": item["question"], "reason": f"unknown source id {sid!r}"})
            else:
                bad = ungrounded_numbers(" ".join(star.values()), sources[sid]["text"])
                if bad:
                    dropped.append({"question": item["question"],
                                    "reason": f"numbers not in {sid}: {', '.join(bad)}"})
                elif any(star.values()):
                    item["source_id"], item["star"] = sid, star
        if item["star"] is None:
            gaps.append({"topic": item["question"],
                         "note": "No profile story answers this. Prepare an honest answer "
                                 "rather than stretching another one."})
        questions.append(item)
    for p in raw.get("probes") or []:
        if len(probes) == MAX_PROBES:
            break
        if not isinstance(p, dict):
            continue
        sid, claim, defence = p.get("source_id"), str(p.get("claim") or "").strip(), \
            str(p.get("defence") or "").strip()
        if not claim or sid not in sources:
            dropped.append({"claim": claim, "reason": f"unknown source id {sid!r}"})
            continue
        bad = ungrounded_numbers(claim + " " + defence, sources[sid]["text"])
        if bad:
            dropped.append({"claim": claim, "reason": f"numbers not in {sid}: {', '.join(bad)}"})
            continue
        probes.append({"claim": claim, "source_id": sid, "defence": defence})
    return {"questions": questions, "probes": probes, "gaps": gaps, "dropped": dropped}


def _money(v) -> str:
    return "unknown" if v is None else f"{v:,} EUR/month"


def to_markdown(pack: dict, *, company: str, role: str, profile: dict, answers: dict,
                jd_missing: bool) -> str:
    sources = profile_sources(profile)
    lines = [f"# Interview prep: {role} at {company}", ""]
    if jd_missing:
        lines += ["> The posting text was not archived; questions are generic for the role.", ""]
    lines += ["## Likely questions", ""]
    for i, q in enumerate(pack["questions"], 1):
        lines.append(f"### {i}. {q['question']}")
        if q["why"]:
            lines.append(f"_Why they ask:_ {q['why']}")
        if q["star"]:
            s = q["star"]
            lines += [f"_Story:_ {sources[q['source_id']]['label']} (`{q['source_id']}`)",
                      f"- **Situation:** {s['situation']}", f"- **Task:** {s['task']}",
                      f"- **Action:** {s['action']}", f"- **Result:** {s['result']}"]
        else:
            lines.append("_No grounded story: see Gaps._")
        lines.append("")
    lines += ["## Claims they will probe", ""]
    lines += [f"- **{p['claim']}** (`{p['source_id']}`): {p['defence']}" for p in pack["probes"]] \
        or ["- none identified"]
    lines += ["", "## Gaps to prepare honestly", ""]
    lines += [f"- **{g['topic']}**: {g['note']}" for g in pack["gaps"]] or ["- none identified"]
    lines += ["", "## Salary rehearsal", ""]
    for label, contractor in (("Employee (gross)", False), ("Contractor (invoiced)", True)):
        r = rehearsal(answers, contractor=contractor)
        if not r["known"]:
            lines.append(f"- {label}: unknown, `{r['floor_key']}` is not set in answers.yaml")
            continue
        lines.append(f"- {label}: anchor {_money(r['anchor'])} ({r['anchor_basis']}), "
                     f"fallback {_money(r['fallback'])}, walk-away {_money(r['walk_away'])}")
    lines += ["", "Say the anchor first, then stop talking. If pushed: \"I'm flexible on the "
              "structure, not on the floor.\""]
    if pack["dropped"]:
        lines += ["", "## Dropped by the fact check", ""]
        lines += [f"- {d.get('question') or d.get('claim')}: {d['reason']}" for d in pack["dropped"]]
    return "\n".join(lines) + "\n"


def generate(*, profile: dict, answers: dict, jd: str, cover_letter: str,
             company: str, role: str, client) -> tuple[str, dict]:
    raw = call_llm(build_messages(profile, jd, cover_letter, company, role), client=client)
    pack = validate(raw, profile)
    md = to_markdown(pack, company=company, role=role, profile=profile, answers=answers,
                     jd_missing=not (jd or "").strip())
    return md, pack


# --- queue glue ----------------------------------------------------------------

def find_entry(scan_date: str, job_id: str, *, queue_dir=None) -> dict:
    path = queue_root(queue_dir) / _validated_day(scan_date) / "jobs.json"
    for e in json.loads(path.read_text()):
        if e.get("id") == job_id:
            return e
    raise KeyError(job_id)


def package_dir_for(entry: dict, scan_date: str, *, queue_dir=None):
    """The entry's package dir; for an entry that never had one (a LinkedIn
    job he applied to by hand) the same place assemble would have used."""
    if entry.get("package_dir"):
        return Path(entry["package_dir"])
    slug = job_slug(entry.get("company") or "unknown", entry.get("title") or "role",
                    on=date.fromisoformat(scan_date))
    return queue_root(queue_dir) / scan_date / "packages" / slug


def prepare(scan_date: str, job_id: str, *, profile: dict, answers: dict, client=None,
            queue_dir=None, force: bool = False):
    """Write interview_prep.md (+ interview_prep.json) into the package dir.
    Returns the .md path. An existing pack is kept unless force."""
    entry = find_entry(scan_date, job_id, queue_dir=queue_dir)
    pkg = package_dir_for(entry, scan_date, queue_dir=queue_dir)
    out = pkg / "interview_prep.md"
    if out.exists() and not force:
        return out
    jd = read_description(scan_date, job_id, queue_dir=queue_dir)
    cover = ""
    for cand in (entry.get("cover_letter_path"), pkg / "cover_letter.md"):
        if cand:
            try:
                cover = Path(cand).read_text()
                break
            except OSError:
                continue
    if client is None:
        from cv_tailor.tailor_llm import build_azure_client
        client = build_azure_client()
    md, pack = generate(profile=profile, answers=answers, jd=jd, cover_letter=cover,
                        company=entry.get("company") or "the company",
                        role=entry.get("title") or "the role", client=client)
    pkg.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    (pkg / "interview_prep.json").write_text(json.dumps(pack, indent=2, ensure_ascii=False))
    return out
