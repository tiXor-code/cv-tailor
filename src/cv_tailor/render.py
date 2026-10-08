"""Jinja2 rendering and WeasyPrint PDF output.

The template needs index lookups for experiences and projects; we build those
maps here so the template stays simple.
"""
import re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

# Acronyms an ATS keyword match (or a recruiter skimming) may search for in
# either form. The ATS-facing templates expand each one ONCE, on its first
# occurrence in body prose ("retrieval-augmented generation (RAG)"); later
# mentions stay short. Definitions only -- this never adds a claim.
ACRONYMS = {
    "LLMs": "large language models",
    "LLM": "large language model",
    "RAG": "retrieval-augmented generation",
    "MCP": "Model Context Protocol",
    "CI/CD": "continuous integration and delivery",
    "HMAC": "hash-based message authentication code",
    "KPIs": "key performance indicators",
    "KPI": "key performance indicator",
    "SEO": "search engine optimization",
}
# Plural and singular forms share one "seen" slot so "KPI" after "KPIs" is not
# expanded a second time.
_ACRONYM_ROOT = {"LLMs": "LLM", "KPIs": "KPI"}
_ACRONYM_RE = re.compile(
    r"(?<![\w/])(" + "|".join(re.escape(a) for a in sorted(ACRONYMS, key=len, reverse=True)) + r")(?![\w/])"
)


class AcronymExpander:
    """Stateful Jinja filter: expands each known acronym the first time it is
    seen across one render. A fresh instance per render keeps documents
    independent."""

    def __init__(self):
        self.seen: set[str] = set()

    def __call__(self, text) -> str:
        if not text:
            return text
        text = str(text)

        def sub(m: re.Match) -> str:
            acro = m.group(1)
            root = _ACRONYM_ROOT.get(acro, acro)
            if root in self.seen:
                return acro
            self.seen.add(root)
            long_form = ACRONYMS[acro]
            # Already spelled out right before it ("large language model (LLM)")?
            before = text[max(0, m.start() - len(long_form) - 3):m.start()].lower()
            if long_form.lower() in before:
                return acro
            return f"{long_form} ({acro})"

        return _ACRONYM_RE.sub(sub, text)


def emphasize(text, phrases=None) -> Markup:
    """Escape `text` and set the first occurrence of each phrase in bold
    (docs/cv-rules.md R8). Phrases come from profile.yaml `emphasis` lists;
    a phrase that is not in the text is skipped, so bolding can never add
    words."""
    out = str(escape(text or ""))
    for phrase in phrases or []:
        safe = str(escape(str(phrase)))
        if safe and safe in out:
            out = out.replace(safe, f"<strong>{safe}</strong>", 1)
    return Markup(out)


def render_html(profile: dict, fields: dict, template_dir: Path | str,
                template_name: str = "cv.html.j2", **context) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["expand_acronyms"] = AcronymExpander()
    env.filters["emphasize"] = emphasize
    template = env.get_template(template_name)
    return template.render(
        profile=profile,
        fields=fields,
        experiences_by_id={e["id"]: e for e in profile.get("experiences", [])},
        projects_by_id={p["id"]: p for p in profile.get("projects", [])},
        **context,
    )


def render_pdf(html: str, css_path: Path | str, out_path: Path | str) -> Path:
    # Lazy import so the rest of the package works without WeasyPrint installed.
    # PDF Title/Author come from the template's <title> and <meta name="author">.
    from weasyprint import HTML, CSS

    out_path = Path(out_path)
    HTML(string=html, base_url=str(Path(css_path).parent)).write_pdf(
        target=str(out_path),
        stylesheets=[CSS(filename=str(css_path))],
    )
    return out_path
