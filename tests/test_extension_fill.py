"""The Scout Fill Chrome extension (extension/), exercised in real Chromium.

Teodor, 2026-09-25: sites that refuse Scout's own browser (Ashby's reCAPTCHA
scoring) are submitted by HIM from his real Chrome; the extension fills the
form first so he types nothing. These tests load a copy of the extension --
patched only to also run on 127.0.0.1 and to talk to a stand-in admin -- into
headless Chromium against the same live-shaped form fixtures the automated
adapters are tested on.

The invariant above everything: the extension NEVER submits. Only after the
test (standing in for Teodor) clicks Submit and the page confirms does it
record "applied".
"""
from __future__ import annotations

import base64
import http.server
import json
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXT_SRC = ROOT / "extension"
FIXTURES = ROOT / "tests" / "fixtures" / "portal"
PDF = b"%PDF-1.4 fixture\n"

# The stand-in for admin's /api/scout/ext/answer: answers by label keyword,
# like Scout's engine would; "notice period" is deliberately unanswerable.
ANSWERS = {
    "name": "Ada Lovelace", "first name": "Ada", "last name": "Lovelace",
    "email": "ada@example.com", "phone": "+44 20 7946 0958", "location": "Exampleton",
    "linkedin": "linkedin.com/in/ada-fixture", "hear": "LinkedIn",
    "salary": "1234 EUR gross per month", "gender": "Decline to self-identify",
    "i agree": "yes", "consent": "yes",
}


def _answer_for(q):
    label = q["label"].lower()
    if "notice period" in label:
        return None
    # longest key first, so "first name" wins over "name"
    for key in sorted(ANSWERS, key=len, reverse=True):
        if key in label:
            return ANSWERS[key]
    if q["kind"] == "radio" and set(q["options"]) >= {"Yes", "No"}:
        return "Yes"
    return None


class _Admin:
    def __init__(self, known_urls):
        self.known_urls = known_urls
        self.applied = []
        self.saved = []
        self.layouts = []
        self.answer_refs = []
        self.questions = []
        self.tokens = []

    @contextmanager
    def serve(self):
        admin = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                admin.tokens.append(self.headers.get("authorization"))
                if self.path.startswith("/api/scout/ext/lookup"):
                    from urllib.parse import parse_qs, urlparse
                    url = parse_qs(urlparse(self.path).query).get("url", [""])[0]
                    hit = any(url.startswith(k) for k in admin.known_urls)
                    return self._json(200, {"job": {"date": "2026-09-25", "id": "job-1", "company": "Fixture Co",
                                                    "title": "AI Engineer"} if hit else None})
                if self.path.startswith("/api/scout/ext/cv"):
                    self.send_response(200)
                    self.send_header("content-type", "application/pdf")
                    self.send_header("content-length", str(len(PDF)))
                    self.end_headers()
                    return self.wfile.write(PDF)
                if self.path.startswith("/confirm.html"):
                    body = (b"<form id=f onsubmit=\"event.preventDefault();document.body.innerHTML="
                            b"'<h1>Thank you for applying!</h1>'\"><label for=n>Name</label><input id=n required>"
                            b"<label for=np>What is your notice period?</label><input id=np required>"
                            b"<button type=submit>Submit application</button></form>")
                    self.send_response(200)
                    self.send_header("content-type", "text/html")
                    self.end_headers()
                    return self.wfile.write(body)
                target = FIXTURES / self.path.split("?")[0].split("#")[0].lstrip("/")
                if target.is_file():
                    data = target.read_bytes()
                    self.send_response(200)
                    self.send_header("content-type", "text/html")
                    self.end_headers()
                    return self.wfile.write(data)
                self._json(404, {})

            def do_POST(self):
                admin.tokens.append(self.headers.get("authorization"))
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                if self.path == "/api/scout/ext/answer":
                    admin.questions.extend(body["questions"])
                    admin.answer_refs.append({k: body[k] for k in ("date", "id") if k in body})
                    answers = [{"label": q["label"], "value": _answer_for(q),
                                "needs_you": _answer_for(q) is None and q["required"]} for q in body["questions"]]
                    return self._json(200, {"ok": True, "cover_letter": "I build fixture agents.", "answers": answers})
                if self.path == "/api/scout/ext/layout":
                    admin.layouts.append(body)
                    return self._json(200, {"ok": True, "file": "layouts/x.html"})
                if self.path == "/api/scout/ext/save-answers":
                    admin.saved.extend(body["answers"])
                    return self._json(200, {"saved": len(body["answers"])})
                if self.path == "/api/scout/ext/applied":
                    admin.applied.append(body)
                    return self._json(200, {"ok": True, "status": "applied_by_hand"})
                self._json(404, {})

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()


def _test_copy(tmp_path, linkedin=False) -> Path:
    ext = tmp_path / "ext"
    shutil.copytree(EXT_SRC, ext)
    content = (ext / "content.js").read_text()
    # tests only: an open shadow root so the test can click the panel like he
    # does, and (LinkedIn tests) 127.0.0.1 treated as linkedin.com
    content = content.replace('attachShadow({ mode: "closed" })', 'attachShadow({ mode: "open" })')
    if linkedin:
        content = content.replace("const LINKEDIN = /(^|\\.)linkedin\\.com$/.test(location.hostname);",
                                  "const LINKEDIN = true;")
        assert "const LINKEDIN = true;" in content
    (ext / "content.js").write_text(content)
    manifest = json.loads((ext / "manifest.json").read_text())
    manifest["host_permissions"].append("http://127.0.0.1/*")
    manifest["content_scripts"][0]["matches"].append("http://127.0.0.1/*")
    (ext / "manifest.json").write_text(json.dumps(manifest))
    return ext


@contextmanager
def _browser(tmp_path, base, token="fixture-key-0123456789abcdef0123456789abcdef", linkedin=False):
    ext = _test_copy(tmp_path, linkedin=linkedin)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(tmp_path / "profile"), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        try:
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
            sw.evaluate("([t, b]) => chrome.storage.local.set({token: t, adminBase: b})", [token, base])
            yield ctx
        finally:
            ctx.close()


def _open(ctx, url):
    page = ctx.new_page()
    page.goto(url, wait_until="load")
    return page


def _wait_filled(page):
    page.wait_for_selector("#scout-fill-panel[data-state=filled]", state="attached", timeout=30000)


def _panel_present(page):
    return page.locator("#scout-fill-panel").count() == 1


def _fill_ashby(ctx, base, variant=""):
    q = f"?variant={variant}" if variant else ""
    page = _open(ctx, f"{base}/ashby_form.html{q}#scout-fill=2026-09-25~job-1")
    _wait_filled(page)
    return page


def test_ashby_form_is_filled_cv_attached_and_never_submitted(tmp_path):
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _fill_ashby(ctx, base)
        assert page.input_value("#_systemfield_name") == "Ada Lovelace"
        assert page.input_value("#_systemfield_email") == "ada@example.com"
        assert page.input_value("#_systemfield_phone") == "+44 20 7946 0958"
        assert page.eval_on_selector("#q_source", "e => e.options[e.selectedIndex].text") == "LinkedIn"
        assert page.eval_on_selector("#_systemfield_resume", "e => e.files.length") == 1
        # the unanswerable required question is left for him, outlined
        assert page.input_value("#q_required_text") == ""
        outline = page.evaluate("() => { const e = document.querySelector('#q_required_text');"
                                " const b = e.closest('.ashby-application-form-field-entry') || e;"
                                " return b.style.outline; }")
        assert "solid" in outline
        # never submitted
        assert page.locator("#confirmation").is_hidden()
        assert admin.applied == []
        assert all(t in (None, "Bearer fixture-key-0123456789abcdef0123456789abcdef") for t in admin.tokens)
        assert "Bearer fixture-key-0123456789abcdef0123456789abcdef" in admin.tokens


def test_ashby_number_consent_yesno_radio_and_swapped_resume(tmp_path):
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _fill_ashby(ctx, base, "salarynumber")
        assert page.input_value("#q_salary_number") == "1234"
        page = _fill_ashby(ctx, base, "consent")
        assert page.eval_on_selector("#q_consent_box", "e => e.checked") is True
        page = _fill_ashby(ctx, base, "resumeswap")
        # Live Ashby swaps the input for an empty clone once the upload lands
        # and shows the filename instead: that is the success signal.
        assert "Teodor-Lutoiu-CV.pdf" in page.inner_text(".resume-dropzone")
        page = _fill_ashby(ctx, base, "radiogroup")
        assert page.evaluate("() => [...document.querySelectorAll('input[name=q_heard_opts]')].some(r => r.checked)")
        page = _fill_ashby(ctx, base, "yesno")
        assert page.evaluate("() => [...document.querySelectorAll('input[type=checkbox]')].some(c => c.checked)"
                             " || [...document.querySelectorAll('button')].some(b => /yes/i.test(b.innerText)"
                             " && (b.getAttribute('aria-pressed') === 'true' || /selected|active/.test(b.className)))")
        assert admin.applied == []


def test_greenhouse_form_contact_questions_and_eeo(tmp_path):
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _open(ctx, f"{base}/greenhouse_form.html?radio=1#scout-fill=2026-09-25~job-1")
        _wait_filled(page)
        assert page.input_value("#first_name") == "Ada"
        assert page.input_value("#last_name") == "Lovelace"
        assert page.input_value("#email") == "ada@example.com"
        assert page.input_value("#question_1002").endswith("linkedin.com/in/ada-fixture")
        assert page.eval_on_selector("#question_1003_d", "e => e.checked") is True
        assert page.eval_on_selector("#resume", "e => e.files.length") == 1
        assert admin.applied == []


def test_a_page_not_on_his_list_stays_untouched(tmp_path):
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _open(ctx, f"{base}/greenhouse_form.html")
        page.wait_for_timeout(2500)
        assert not _panel_present(page)
        assert page.input_value("#first_name") == ""
        assert admin.questions == []


def test_a_page_on_his_list_without_the_link_offers_but_does_not_auto_fill(tmp_path):
    admin = _Admin(["http://127.0.0.1"])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _open(ctx, f"{base}/greenhouse_form.html")
        page.wait_for_timeout(2500)
        assert _panel_present(page)
        assert page.input_value("#first_name") == ""  # waits for his click on "Fill this form"


def test_applied_is_recorded_only_after_he_submits_and_the_site_confirms(tmp_path):
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _open(ctx, f"{base}/confirm.html#scout-fill=2026-09-25~job-1")
        _wait_filled(page)
        assert admin.applied == []            # filled, not submitted, nothing recorded
        page.fill("#np", "Fixture weeks")      # the one question only he can answer
        page.click("button[type=submit]")      # Teodor presses Submit
        page.wait_for_timeout(3000)
        assert admin.applied == [{"date": "2026-09-25", "id": "job-1"}]


def test_a_confirmation_text_without_his_submit_is_not_recorded(tmp_path):
    """A page that merely SAYS 'thank you for applying' (a marketing banner,
    a previous session) must not tick applied: he has to have pressed Submit."""
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _open(ctx, f"{base}/confirm.html#scout-fill=2026-09-25~job-1")
        _wait_filled(page)
        page.evaluate("() => document.body.append('Thank you for applying!')")
        page.wait_for_timeout(3000)
        assert admin.applied == []


def test_what_he_typed_is_saved_when_he_submits(tmp_path):
    """The notice period is unanswerable: he types it, submits, the site
    confirms -- and his answer is saved so no form asks him again."""
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base) as ctx:
        page = _open(ctx, f"{base}/confirm.html#scout-fill=2026-09-25~job-1")
        _wait_filled(page)
        assert page.input_value("#np") == ""
        page.fill("#np", "Four fixture weeks")
        assert admin.saved == []                  # nothing saved behind his back
        page.click("button[type=submit]")
        page.wait_for_timeout(3000)
        assert admin.saved == [{"label": "What is your notice period?", "value": "Four fixture weeks"}]
        assert admin.applied == [{"date": "2026-09-25", "id": "job-1"}]



# --- LinkedIn Easy Apply (Teodor, 2026-09-25: "do the same thing for
# linkedin easy apply"). Replica fixture; see its header. ---------------------

def _panel_button(page, text):
    return page.locator("#scout-fill-panel").locator(f"button:has-text('{text}')")


def _open_easy_apply(ctx, base):
    page = _open(ctx, f"{base}/linkedin_easyapply.html")
    page.wait_for_timeout(1500)
    assert not _panel_present(page), "no panel while he just browses LinkedIn"
    page.click("#easy-apply")
    page.wait_for_selector("#scout-fill-panel", state="attached", timeout=10000)
    return page


def test_linkedin_nothing_happens_until_he_clicks_fill(tmp_path):
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base, linkedin=True) as ctx:
        page = _open_easy_apply(ctx, base)
        page.wait_for_timeout(2500)
        assert page.input_value("#phone") == ""
        assert admin.questions == []


def test_linkedin_steps_filled_never_advanced_and_applied_after_his_submit(tmp_path):
    admin = _Admin(["http://127.0.0.1"])     # this job IS on his list
    with admin.serve() as base, _browser(tmp_path, base, linkedin=True) as ctx:
        page = _open_easy_apply(ctx, base)
        _panel_button(page, "Fill this step").click()
        _wait_filled(page)
        assert page.input_value("#phone") == "+44 20 7946 0958"
        assert page.eval_on_selector("#email", "e => e.options[e.selectedIndex].text") == "ada@example.com"
        assert page.eval_on_selector("#auth-yes", "e => e.checked") is True
        assert page.input_value("#years") == ""              # only he knows: outlined
        page.wait_for_timeout(2500)
        assert page.evaluate("() => window.__step()") == 0   # it never presses Next
        page.fill("#years", "3")
        page.click("#next")                                  # HE presses Next
        page.wait_for_function("() => document.querySelector('#why') && document.querySelector('#why').value !== ''",
                               timeout=15000)                # step 2 filled once, by itself
        assert page.input_value("#why") == "I build fixture agents."
        assert "Teodor-Lutoiu-CV.pdf" in page.inner_text(".resume-name")
        assert page.evaluate("() => window.__step()") == 1
        page.click("#next")                                  # Review
        page.wait_for_timeout(1500)
        assert admin.applied == []
        page.click("#next")                                  # HE presses Submit application
        page.wait_for_timeout(3000)
        assert admin.applied == [{"date": "2026-09-25", "id": "job-1"}]
        assert {"label": "How many years of work experience do you have with Fixturelang?", "value": "3"} in admin.saved
        assert all(ref == {"date": "2026-09-25", "id": "job-1"} for ref in admin.answer_refs)


def test_linkedin_job_not_on_his_list_is_filled_from_profile_without_a_cv(tmp_path):
    admin = _Admin([])                                  # not on his list
    with admin.serve() as base, _browser(tmp_path, base, linkedin=True) as ctx:
        page = _open_easy_apply(ctx, base)
        _panel_button(page, "Fill this step").click()
        _wait_filled(page)
        assert page.input_value("#phone") == "+44 20 7946 0958"
        assert admin.answer_refs and all(ref == {} for ref in admin.answer_refs)
        page.fill("#years", "3")
        page.click("#next")
        page.wait_for_timeout(3000)
        assert page.eval_on_selector("#resume", "e => e.files.length") == 0   # no tailored CV to give
        page.click("#next")
        page.click("#next")
        page.wait_for_timeout(3000)
        assert admin.applied == []                          # nothing on his list to tick
        assert any(s["value"] == "3" for s in admin.saved)  # but his typed answer is kept


def test_layout_report_strips_values(tmp_path):
    admin = _Admin([])
    with admin.serve() as base, _browser(tmp_path, base, linkedin=True) as ctx:
        page = _open_easy_apply(ctx, base)
        page.fill("#years", "SECRET-VALUE-42")
        _panel_button(page, "Send this form").click()
        page.wait_for_timeout(2000)
        assert len(admin.layouts) == 1
        html = admin.layouts[0]["html"]
        assert "How many years of work experience" in html and "SECRET-VALUE-42" not in html
        assert "scout-fill-panel" not in html
