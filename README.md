# cv-tailor

Takes a job description and a structured profile, returns a tailored PDF CV. A single Azure OpenAI call returns structured JSON that drives a deterministic Jinja/WeasyPrint renderer. No LLM in the layout path. An honesty-guard prompt blocks fabricated experience, projects, or skills.

## How it works

```
JD + profile.yaml
      |
      v
Azure OpenAI (single call, structured JSON output)
      |
      v
Jinja2 template (cv.html.j2)
      |
      v
WeasyPrint renderer
      |
      v
Tailored PDF
```

The LLM selects and rewrites from what is already in `profile.yaml`. It cannot invent new experience or projects. A system-prompt honesty guard enforces this constraint and the output is validated against the profile before rendering.

## Honesty guard

The system prompt explicitly instructs the model to select only experience, projects, and skills that exist in the profile. A post-LLM validation step cross-references every ID in the returned JSON against the profile and raises an error if anything was fabricated. This makes the output safe to send to employers.

## Tests

43 tests across 11 modules covering the LLM call, renderer, ATS checker, profile loader, validator, slug generator, job-source reader, CRM helpers, Sheets writer, and end-to-end smoke tests.

Run the unit tests (no external services needed):

```bash
pytest -m "not integration"
```

Run everything including integration tests (requires `.env`):

```bash
pytest
```

## Setup

1. Copy `.env.example` to `.env` and fill in your Azure OpenAI credentials and Google Sheets service account path.
2. Install Python dependencies:

```bash
pip install -r requirements.txt
```

3. WeasyPrint requires system-level libraries (Cairo, Pango, GDK-PixBuf). On macOS: `brew install weasyprint`. On Debian/Ubuntu: `apt-get install python3-weasyprint` or follow the [WeasyPrint installation docs](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html#installation).

4. Run against a job description:

```bash
python scripts/tailor.py --jd path/to/jd.txt
```

## License

MIT - see [LICENSE](LICENSE).
