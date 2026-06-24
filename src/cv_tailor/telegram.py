"""Send the weekly scan digest to a Telegram chat via Bot API.

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment. If either is
missing, send_digest is a no-op so the scanner still works without Telegram.
"""
import json
import os
import urllib.parse
import urllib.request
from typing import Optional


MAX_MESSAGE = 3800  # leave headroom under Telegram's 4096 char limit


def send_text(text: str, *, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """Send a plain-text message. Returns True on success, False on failure or
    missing credentials. Splits long messages into chunks."""
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False

    chunks = _chunk_text(text, MAX_MESSAGE)
    ok = True
    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_data = json.load(resp)
                if not resp_data.get("ok"):
                    ok = False
        except Exception:
            ok = False
    return ok


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text on line boundaries to stay under `limit` chars per chunk."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def format_digest_for_telegram(scored: list[dict], scan_date) -> str:
    """Compact digest format optimized for phone reading."""
    if not scored:
        return f"cv-tailor scan {scan_date}: 0 new candidates."
    lines = [f"cv-tailor scan — {scan_date}", f"{len(scored)} new candidates", ""]
    for i, item in enumerate(scored, 1):
        job = item["job"]
        lines.append(f"{i}. [{item['score']}/10] {job.org} — {job.title}")
        lines.append(f"   {job.location}")
        lines.append(f"   Why: {item['reason']}")
        lines.append(f"   {job.url}")
        lines.append("")
    lines.append("Review & apply: https://admin.teodorlutoiu.com/scout")
    return "\n".join(lines)
