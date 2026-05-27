"""Format a list of scored job candidates into a markdown digest."""
from datetime import date


def format_digest(scored: list[dict], scan_date: date | None = None) -> str:
    """scored = [{'job': JobPosting, 'score': int, 'reason': str, 'keywords': [...]}, ...] sorted desc."""
    scan_date = scan_date or date.today()
    lines = [
        f"# Job scan — {scan_date.isoformat()}",
        "",
        f"{len(scored)} candidates after filtering. Listed highest-fit first.",
        "",
    ]
    for i, item in enumerate(scored, 1):
        job = item["job"]
        lines.append(f"## {i}. {job.org} — {job.title}  ·  score {item['score']}/10")
        lines.append(f"- **Location:** {job.location}")
        lines.append(f"- **URL:** {job.url}")
        lines.append(f"- **Why:** {item['reason']}")
        if item.get("keywords"):
            lines.append(f"- **Matched:** {', '.join(item['keywords'])}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("To approve specific candidates for CV generation, run:")
    lines.append("")
    lines.append(f"    python scripts/process_approved.py {scan_date.isoformat()} 1,3,5")
    return "\n".join(lines)
