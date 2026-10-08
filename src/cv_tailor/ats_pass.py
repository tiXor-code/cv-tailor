"""Post-render ATS + JD-coverage pass for assemble_package.

After the CV is rendered: simulate an ATS over every CV file in the package
(ats_sim) and measure the CV text's coverage of the JD's salient terms
(jd_terms). If some missing terms are SUPPORTED by profile.yaml, run ONE more
tailoring pass that names them as a hint, push the result through the same
honesty guard, re-render, and keep it only if it validates and doesn't lower
coverage. Unsupported terms are never hinted -- they stay honest gaps.

This pass can only improve or leave a package unchanged: any failure in it
(LLM error, a second-pass honesty violation, a missing pdftotext) keeps the
first-pass fields and is recorded in meta, never raised.
"""
from __future__ import annotations

import html as html_lib
import re
import sys
from pathlib import Path
from typing import Callable

from cv_tailor.ats_sim import package_result, simulate_file
from cv_tailor.jd_terms import apply_judgement, coverage, judge_support

HINT_TEMPLATE = """

# Tailoring hint (second pass)
These job-description terms are TRUE per the candidate profile above but are
missing from the first-draft CV: {terms}.
Work them in honestly by choosing the experiences, bullets, projects, skills and
summary wording from the profile that already state them. Every honesty rule
still applies: do not invent anything, and skip any term you cannot ground in
the profile text.
"""


def cv_files(pkg_dir: Path) -> list[Path]:
    """Every CV variant in a package (cv.pdf, cv-*.pdf, cv*.docx)."""
    return sorted(pkg_dir.glob("cv*.pdf")) + sorted(pkg_dir.glob("cv*.docx"))


def _html_text(path: Path) -> str:
    if not path.exists():
        return ""
    body = re.sub(r"<(script|style|head)\b.*?</\1>", " ", path.read_text(), flags=re.DOTALL | re.I)
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", body))


def measure(pkg_dir: Path, profile: dict, jd_for_terms: str, *, exclude=()) -> tuple[dict, dict]:
    """(ats package result, jd coverage) for the package as it is on disk.
    Coverage reads the text an ATS would parse; if no CV file yields text
    (e.g. pdftotext missing) it falls back to the rendered cv.html."""
    reports = [simulate_file(p, profile=profile) for p in cv_files(pkg_dir)]
    ats = package_result(reports)
    text = "\n".join(r.text for r in reports if r.text) or _html_text(pkg_dir / "cv.html")
    return ats, coverage(jd_for_terms, text, profile, exclude=exclude)


def _ats_meta(ats: dict) -> dict:
    """meta.json view of an ats_sim result: scores and checks, no parsed text."""
    return {
        "score": ats["score"],
        "files": [{"path": Path(f["path"]).name, "score": f["score"], "checks": f["checks"]}
                  for f in ats["files"]],
        "cross_file": ats["cross_file"],
    }


def run_pass(
    *,
    profile: dict,
    tailor_profile: dict,
    fields: dict,
    jd_text: str,
    jd_body: str,
    company: str,
    role: str,
    pkg_dir: Path,
    client,
    tailor_fn: Callable,
    validate_fn: Callable,
    render_fn: Callable[[dict], None],
) -> tuple[dict, dict]:
    """Returns (fields to use from here on, meta additions). render_fn(fields)
    must re-render every CV file of the package from the given fields."""
    jd_for_terms = f"{role}\n{jd_body}"
    exclude = [company]
    try:
        ats, cov = measure(pkg_dir, profile, jd_for_terms, exclude=exclude)
    except Exception as exc:  # noqa: BLE001 -- scoring must never sink a package
        print(f"ats pass failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return fields, {"ats": {"error": f"{type(exc).__name__}: {exc}"}}

    # literal misses the LLM finds the profile supports (verbatim evidence only)
    support = judge_support(cov["missing_unsupported"], profile, client=client)
    cov = apply_judgement(cov, support)

    refine = {"attempted": False, "adopted": False, "first_pass_match_pct": cov["match_pct"]}
    if cov["missing_supported"]:
        refine["attempted"] = True
        refine["hinted_terms"] = list(cov["missing_supported"])
        new_fields, dropped, reason = _second_pass(
            profile, tailor_profile, fields, jd_text, cov["missing_supported"],
            client=client, tailor_fn=tailor_fn, validate_fn=validate_fn)
        if new_fields is None:
            refine["reason"] = reason
        else:
            try:
                render_fn(new_fields)
                new_ats, new_cov = measure(pkg_dir, profile, jd_for_terms, exclude=exclude)
            except Exception as exc:  # noqa: BLE001
                new_ats, new_cov = None, None
                refine["reason"] = f"second-pass render failed: {type(exc).__name__}: {exc}"
            if new_cov is not None:
                new_cov = apply_judgement(new_cov, support)
            if new_cov is not None and new_cov["match_pct"] >= cov["match_pct"]:
                fields, ats, cov = new_fields, new_ats, new_cov
                refine["adopted"] = True
                refine["skills_dropped"] = dropped
            else:
                refine.setdefault("reason", "second pass lowered JD coverage")
                render_fn(fields)  # put the first-pass files back

    return fields, {
        "ats": _ats_meta(ats),
        "jd_match_pct": cov["match_pct"],
        "jd_missing_supported": cov["missing_supported"],
        "jd_missing_unsupported": cov["missing_unsupported"],
        "jd_support_evidence": support,
        "ats_refine": refine,
    }


def _second_pass(profile, tailor_profile, fields, jd_text, terms, *, client, tailor_fn,
                 validate_fn) -> tuple[dict | None, list, str]:
    """One hinted re-tailor, post-processed like the first pass in
    assemble_package (job_meta and skills_groups carried over, unfinished
    projects dropped, non-profile skills dropped, programming languages never
    bolded) and validated. Keep this in step with assemble_package.
    Returns (fields, skills_dropped, "") or (None, [], why it was rejected)."""
    from cv_tailor.assemble import drop_unfinished_projects  # lazy: assemble imports us
    from cv_tailor.cv_rules import apply_cv_rules

    try:
        new = tailor_fn(tailor_profile, jd_text + HINT_TEMPLATE.format(terms=", ".join(terms)),
                        client=client)
    except Exception as exc:  # noqa: BLE001
        return None, [], f"second-pass tailor failed: {type(exc).__name__}: {exc}"
    if not isinstance(new, dict):
        return None, [], "second-pass tailor returned no JSON object"
    new["job_meta"] = dict(fields.get("job_meta", {}))
    # Same headline/summary repair as the first pass (docs/cv-rules.md).
    new["_cv_rule_fixes"] = apply_cv_rules(profile, new)
    new["project_ids"] = drop_unfinished_projects(profile, new.get("project_ids", []))
    if "skills_groups" in fields:
        new["skills_groups"] = list(fields["skills_groups"])
    canon = {s.lower(): s for grp in profile.get("skills", {}).values() for s in grp}
    emphasis = [canon.get(str(s).lower(), s) for s in new.get("skills_emphasis", [])]
    coding = {s.lower() for s in (profile.get("skills", {}) or {}).get("languages", []) or []}
    new["skills_emphasis"] = [s for s in emphasis
                              if str(s).lower() in canon and str(s).lower() not in coding]
    errors = validate_fn(profile, new)
    if errors:
        return None, [], "second pass failed the honesty guard: " + "; ".join(errors)
    return new, [s for s in emphasis if str(s).lower() not in canon], ""
