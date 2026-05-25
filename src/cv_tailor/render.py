"""Jinja2 rendering and WeasyPrint PDF output.

The template needs index lookups for experiences and projects; we build those
maps here so the template stays simple.
"""
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape


def render_html(profile: dict, fields: dict, template_dir: Path | str) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("cv.html.j2")
    return template.render(
        profile=profile,
        fields=fields,
        experiences_by_id={e["id"]: e for e in profile.get("experiences", [])},
        projects_by_id={p["id"]: p for p in profile.get("projects", [])},
    )


def render_pdf(html: str, css_path: Path | str, out_path: Path | str) -> Path:
    # Lazy import so the rest of the package works without WeasyPrint installed.
    from weasyprint import HTML, CSS

    out_path = Path(out_path)
    HTML(string=html, base_url=str(Path(css_path).parent)).write_pdf(
        target=str(out_path),
        stylesheets=[CSS(filename=str(css_path))],
    )
    return out_path
