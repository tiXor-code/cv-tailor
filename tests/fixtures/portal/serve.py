# tests/fixtures/portal/serve.py
"""Serve this directory's fixture HTML over http.server on an ephemeral
localhost port, for real-Playwright tests that need an actual http:// origin
(file:// origins behave differently for some form/iframe semantics).

    from fixtures.portal.serve import serve_fixtures

    with serve_fixtures() as base_url:
        page.goto(f"{base_url}/simple_form.html")
"""
from __future__ import annotations

import contextlib
import functools
import http.server
import threading
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent


@contextlib.contextmanager
def serve_fixtures():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
