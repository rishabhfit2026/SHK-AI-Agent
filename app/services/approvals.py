"""Human approval steps: quotes needing sign-off and the Outbox of buyer messages."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Outbox, Quote
from app.services.quote_pdf import render_quote_pdf
from app.util import log_activity


def approve_quote(db: Session, quote: Quote, user: str, discount_note: str = "") -> None:
    if quote.status != "pending_approval":
        raise ValueError(f"Quote {quote.number} is {quote.status}")
    quote.status, quote.approved_by = "approved", user
    if discount_note:
        quote.notes = (quote.notes + "\n" + discount_note).strip()
    path = render_quote_pdf(db, quote)
    # Release the waiting customer message with the PDF attached.
    for item in db.scalars(select(Outbox).where(Outbox.quote_id == quote.id, Outbox.status == "pending_approval")):
        item.attachment_path, item.status, item.decided_by = str(path), "approved", user
        quote.status = "sent"
    if quote.lead_id:
        log_activity(db, "lead", quote.lead_id, "quote", f"{quote.number} approved by {user}", user)
    db.flush()


def reject_quote(db: Session, quote: Quote, user: str, reason: str = "") -> None:
    quote.status, quote.approved_by = "rejected", user
    quote.notes = (quote.notes + f"\nRejected by {user}: {reason}").strip()
    for item in db.scalars(select(Outbox).where(Outbox.quote_id == quote.id, Outbox.status == "pending_approval")):
        item.status, item.decided_by = "rejected", user
    db.flush()


def decide_outbox(db: Session, item: Outbox, approve: bool, user: str, body: str | None = None) -> None:
    if item.status != "pending_approval":
        raise ValueError(f"Message is already {item.status}")
    if item.quote_id and approve:
        quote = db.get(Quote, item.quote_id)
        if quote and quote.status == "pending_approval":
            approve_quote(db, quote, user)
            return
    if body is not None and body.strip():
        item.body = body.strip()
    item.status = "approved" if approve else "rejected"
    item.decided_by = user
    db.flush()


def buyer_accepts(db: Session, quote: Quote, note: str = "", via: str = "chat") -> None:
    """The buyer confirmed a quote; sales confirms the order, PO and payment."""
    from app.services.crm import create_task

    if quote.status not in ("approved", "sent"):
        raise ValueError(f"Quote is {quote.status.replace('_', ' ')} and can't be accepted")
    quote.status = "accepted"
    lead = quote.lead
    rep_id = lead.rep_id if lead else (quote.customer.rep_id if quote.customer else None)
    create_task(db, kind="followup", title=f"Buyer accepted {quote.number} ({quote.party_name}): confirm order",
                details=f"Collect PO / advance and create the order. Buyer note: {note}",
                lead_id=quote.lead_id, customer_id=quote.customer_id, rep_id=rep_id)
    if lead:
        log_activity(db, "lead", lead.id, "accepted", f"Accepted {quote.number} via {via}")
    db.flush()
