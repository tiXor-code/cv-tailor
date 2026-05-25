"""Validate the LLM's `fields.json` output against the profile.

Returns a list of human-readable error strings. Empty list = valid.
"""


def validate(profile: dict, fields: dict) -> list[str]:
    errors: list[str] = []

    # Summary id must exist.
    summary_ids = {s["id"] for s in profile.get("summary_pool", [])}
    if fields.get("chosen_summary_id") not in summary_ids:
        errors.append(
            f"chosen_summary_id '{fields.get('chosen_summary_id')}' not in profile.summary_pool"
        )

    # Experience ids and bullet indices.
    exp_by_id = {e["id"]: e for e in profile.get("experiences", [])}
    for exp_id in fields.get("experience_ids_ordered", []):
        if exp_id not in exp_by_id:
            errors.append(f"experience_id '{exp_id}' not in profile.experiences")

    for exp_id, indices in fields.get("experience_bullets", {}).items():
        if exp_id not in exp_by_id:
            errors.append(
                f"experience_bullets references unknown experience '{exp_id}'"
            )
            continue
        bullet_count = len(exp_by_id[exp_id].get("bullets", []))
        for idx in indices:
            if not isinstance(idx, int) or idx < 0 or idx >= bullet_count:
                errors.append(
                    f"experience '{exp_id}' bullet index {idx} out of range "
                    f"(0..{bullet_count - 1})"
                )

    # Project ids.
    project_ids = {p["id"] for p in profile.get("projects", [])}
    for pid in fields.get("project_ids", []):
        if pid not in project_ids:
            errors.append(f"project_id '{pid}' not in profile.projects")

    # Skills emphasis must appear somewhere in profile.skills.*
    all_skills: set[str] = set()
    for group in profile.get("skills", {}).values():
        for s in group:
            all_skills.add(s)
    for s in fields.get("skills_emphasis", []):
        if s not in all_skills:
            errors.append(f"skills_emphasis '{s}' not in profile.skills")

    return errors
