"""Outbound email over SMTP. With SMTP_HOST unset, emails are only logged (dry-run)."""

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.config import settings

log = logging.getLogger(__name__)


def send_email(to: str, subject: str, body: str, attachment: Path | None = None) -> str:
    if not settings.email_enabled:
        log.info("[dry-run] email to %s: %s", to, subject)
        return "dry-run"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    if attachment and attachment.exists():
        msg.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf", filename=attachment.name)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(msg)
    return "sent"
