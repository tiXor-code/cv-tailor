# src/cv_tailor/sender.py
"""Gated SMTP sender for the Scout email-apply loop.

Safety is the whole point of this module. All gates live here (not in the
caller): a missing apply_target blocks, a duplicate (job_id or normalized
company+role) blocks, an armed send over the daily cap blocks. Unarmed runs
always preview to the sender's own inbox and never touch the dedupe ledger,
so a job stays truly sendable once armed.

Gate order: apply_target present -> duplicate -> (armed only) daily cap ->
compose -> send -> (armed only) record_application.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from cv_tailor.cache import application_exists, applications_sent_today, record_application

SIGNATURE_FIELDS = ("name", "phone", "email", "website", "linkedin", "github")
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587
MAX_SLUG_LEN = 40


class SendResult(NamedTuple):
    status: str    # "sent" | "preview_sent" | "blocked"
    recipient: str  # actual RCPT used ("" when blocked)
    reason: str    # "" on success; "duplicate" | "daily-cap" | error text


def _company_slug(company: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", company or "").strip("-")
    return slug[:MAX_SLUG_LEN]


def _signature_block(contact: dict) -> str:
    contact = contact or {}
    return "\n".join(str(contact[field]) for field in SIGNATURE_FIELDS if contact.get(field))


def _compose_message(*, entry: dict, pkg_dir: Path, profile: dict,
                      from_addr: str, recipient: str, preview: bool) -> MIMEMultipart:
    title = entry.get("title", "")
    company = entry.get("company", "")
    base_subject = f"Application for {title} - Teodor-Cristian Lutoiu"
    subject = f"[PREVIEW] {base_subject}" if preview else base_subject

    cover_text = (pkg_dir / "cover_letter.md").read_text(encoding="utf-8").strip()
    signature = _signature_block(profile.get("contact", {}))
    body = f"{cover_text}\n\n--\n{signature}\n"

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    pdf_bytes = (pkg_dir / "cv.pdf").read_bytes()
    filename = f"Teodor-Lutoiu-CV-{_company_slug(company)}.pdf"
    attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(attachment)
    return msg


def _default_smtp_factory():
    return smtplib.SMTP(DEFAULT_SMTP_HOST, DEFAULT_SMTP_PORT, timeout=30)


def send_application(entry: dict, pkg_dir: Path, profile: dict, *, conn,
                      smtp_factory: Optional[Callable[[], object]] = None) -> SendResult:
    apply_target = (entry.get("apply_target") or "").strip()
    if not apply_target:
        return SendResult(status="blocked", recipient="",
                           reason="missing apply_target on entry")

    job_id = entry.get("id", "")
    company = entry.get("company", "")
    role = entry.get("title", "")

    if application_exists(conn, job_id=job_id, company=company, role=role):
        return SendResult(status="blocked", recipient="", reason="duplicate")

    armed = os.environ.get("APPLY_ARMED", "0") == "1"

    if armed:
        cap = int(os.environ.get("APPLY_DAILY_CAP", "10"))
        if applications_sent_today(conn) >= cap:
            return SendResult(status="blocked", recipient="", reason="daily-cap")

    smtp_user = os.environ.get("APPLY_SMTP_USER", "")
    smtp_password = os.environ.get("APPLY_SMTP_PASSWORD", "")
    recipient = apply_target if armed else smtp_user

    msg = _compose_message(entry=entry, pkg_dir=pkg_dir, profile=profile,
                            from_addr=smtp_user, recipient=recipient, preview=not armed)

    factory = smtp_factory or _default_smtp_factory
    smtp = factory()
    smtp.starttls()
    smtp.login(smtp_user, smtp_password)
    smtp.sendmail(smtp_user, [recipient], msg.as_string())
    smtp.quit()

    if armed:
        record_application(conn, job_id=job_id, company=company, role=role,
                            url=entry.get("url", ""), channel=entry.get("apply_method", "email"))
        return SendResult(status="sent", recipient=recipient, reason="")

    return SendResult(status="preview_sent", recipient=recipient, reason="")
