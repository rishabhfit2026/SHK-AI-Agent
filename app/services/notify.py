"""Delivery of approved Outbox messages: portal inbox first, email as a copy."""

import logging
import smtplib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.email import send_email
from app.config import settings
from app.models import Customer, Lead, Notification, Outbox, PortalUser
from app.util import now

log = logging.getLogger(__name__)

SUBJECTS = {
    "quote": "Your quotation from Shri Hare-Krishna Sponge Iron",
    "payment_reminder": "Payment reminder - Shri Hare-Krishna Sponge Iron",
    "reorder": "Do you need material this week? - Shri Hare-Krishna Sponge Iron",
    "dispatch": "Dispatch update - Shri Hare-Krishna Sponge Iron",
    "outreach": "Shri Hare-Krishna Sponge Iron, Raipur",
    "brief": "SHK daily sales brief",
}


def recipient_email(db: Session, item: Outbox) -> str:
    """Explicit address, else the customer's or lead's portal login / record email."""
    if item.to_email:
        return item.to_email
    if item.customer_id:
        user = db.scalar(select(PortalUser).where(PortalUser.customer_id == item.customer_id, PortalUser.active.is_(True)))
        if user:
            return user.email
        cust = db.get(Customer, item.customer_id)
        if cust and cust.email:
            return cust.email
    if item.lead_id:
        user = db.scalar(select(PortalUser).where(PortalUser.lead_id == item.lead_id, PortalUser.active.is_(True)))
        if user:
            return user.email
        lead = db.get(Lead, item.lead_id)
        if lead and lead.email:
            return lead.email
    return ""


def has_portal_login(db: Session, customer_id: int | None, lead_id: int | None) -> bool:
    q = select(PortalUser.id).where(PortalUser.active.is_(True))
    if customer_id:
        return bool(db.scalar(q.where(PortalUser.customer_id == customer_id)))
    if lead_id:
        return bool(db.scalar(q.where(PortalUser.lead_id == lead_id)))
    return False


def deliver(db: Session, item: Outbox) -> None:
    subject = item.subject or SUBJECTS.get(item.kind, "Message from Shri Hare-Krishna Sponge Iron")
    notes = []
    if item.customer_id or item.lead_id:
        db.add(Notification(customer_id=item.customer_id, lead_id=item.lead_id, kind=item.kind, title=subject,
                            body=item.body, quote_id=item.quote_id, attachment_path=item.attachment_path))
        if not has_portal_login(db, item.customer_id, item.lead_id):
            notes.append("saved to portal inbox, but this buyer has no portal login yet")
    email = recipient_email(db, item)
    try:
        if email:
            body = item.body
            if item.customer_id or item.lead_id:
                body += f"\n\nView your account: {settings.public_base_url}/portal"
            result = send_email(email, subject, body, Path(item.attachment_path) if item.attachment_path else None)
            if result == "dry-run":
                notes.append("email dry-run (SMTP not configured)")
        elif not (item.customer_id or item.lead_id):
            raise ValueError("no portal account or email address for this recipient")
        else:
            notes.append("no email address")
        item.status, item.sent_at, item.error = "sent", now(), "; ".join(notes)
    except (smtplib.SMTPException, OSError, ValueError) as e:
        item.status, item.error = "failed", str(e)
        log.error("Outbox %s failed: %s", item.id, e)


def flush_outbox(db: Session) -> dict:
    sent = failed = 0
    for item in db.scalars(select(Outbox).where(Outbox.status == "approved").order_by(Outbox.id)):
        deliver(db, item)
        if item.status == "sent":
            sent += 1
        else:
            failed += 1
    db.flush()
    return {"sent": sent, "failed": failed}
