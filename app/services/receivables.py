"""Outstanding invoices, ageing and payment reminders (module 4)."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Customer, Invoice, Outbox, Payment
from app.services.crm import create_task
from app.util import inr, today

BUCKETS = ("not_due", "0-30", "31-60", "61-90", "90+")

# Days relative to due date on which a reminder is drafted, and its tone.
REMINDER_STEPS = {
    -3: "friendly",
    0: "due_today",
    7: "overdue",
    15: "firm",
    30: "escalate",
}


def bucket(inv: Invoice, on: date) -> str:
    overdue = (on - inv.due_date).days
    if overdue <= 0:
        return "not_due"
    if overdue <= 30:
        return "0-30"
    if overdue <= 60:
        return "31-60"
    if overdue <= 90:
        return "61-90"
    return "90+"


def open_invoices(db: Session, customer_id: int | None = None) -> list[Invoice]:
    q = select(Invoice).where(Invoice.amount > Invoice.paid_amount)
    if customer_id is not None:
        q = q.where(Invoice.customer_id == customer_id)
    return list(db.scalars(q.order_by(Invoice.due_date)))


@dataclass
class AgeingRow:
    customer: Customer
    buckets: dict[str, float]

    @property
    def total(self) -> float:
        return round(sum(self.buckets.values()), 2)

    @property
    def overdue(self) -> float:
        return round(sum(v for k, v in self.buckets.items() if k != "not_due"), 2)


def ageing(db: Session, on: date | None = None) -> list[AgeingRow]:
    on = on or today()
    rows: dict[int, AgeingRow] = {}
    for inv in open_invoices(db):
        row = rows.setdefault(inv.customer_id, AgeingRow(inv.customer, {b: 0.0 for b in BUCKETS}))
        row.buckets[bucket(inv, on)] += inv.outstanding
    return sorted(rows.values(), key=lambda r: r.overdue, reverse=True)


def statement_text(db: Session, customer: Customer, on: date | None = None) -> str:
    on = on or today()
    invs = open_invoices(db, customer.id)
    if not invs:
        return "No outstanding invoices. Thank you!"
    lines = []
    for inv in invs:
        days = (on - inv.due_date).days
        state = f"overdue {days} days" if days > 0 else f"due {inv.due_date:%d-%b}"
        lines.append(f"{inv.number} dated {inv.invoice_date:%d-%b}: {inr(inv.outstanding)} ({state})")
    total = sum(i.outstanding for i in invs)
    return "\n".join(lines + [f"Total outstanding: {inr(total)}"])


def _reminder_body(customer: Customer, inv: Invoice, tone: str, days: int) -> str:
    amt = inr(inv.outstanding)
    head = f"Dear {customer.name},"
    if tone == "friendly":
        msg = f"a gentle reminder that invoice {inv.number} for {amt} is due on {inv.due_date:%d-%b-%Y}."
    elif tone == "due_today":
        msg = f"invoice {inv.number} for {amt} is due today. Kindly arrange payment."
    elif tone == "overdue":
        msg = f"invoice {inv.number} for {amt} is overdue by {days} days. Please share the payment date."
    else:
        msg = (
            f"invoice {inv.number} for {amt} is overdue by {days} days. Please clear it at the earliest "
            f"so that dispatches continue without interruption."
        )
    return f"{head} {msg}\nIf already paid, please reply with the UTR so we can update our records.\n- Accounts, Shri Hare-Krishna Sponge Iron Ltd"


def run_payment_reminders(db: Session, on: date | None = None) -> dict:
    """Daily job: draft reminders on the scheduled days; escalate long-overdue ones to the rep."""
    on = on or today()
    drafted = escalated = 0
    for inv in open_invoices(db):
        days = (on - inv.due_date).days
        tone = REMINDER_STEPS.get(days)
        if tone is None:
            continue
        key = f"pay:{inv.id}:{days}"
        if db.scalar(select(Outbox.id).where(Outbox.dedupe_key == key)):
            continue
        cust = inv.customer
        if tone == "escalate":
            if create_task(
                db, kind="payment_followup",
                title=f"{cust.name}: {inv.number} overdue {days} days ({inr(inv.outstanding)})",
                details="Call the owner. Consider holding further dispatches until cleared.",
                dedupe_key=key, customer_id=cust.id, rep_id=cust.rep_id, due_date=on,
            ):
                escalated += 1
        db.add(Outbox(
            to_phone=cust.phone, to_name=cust.name, kind="payment_reminder",
            customer_id=cust.id, dedupe_key=key, body=_reminder_body(cust, inv, tone, days),
        ))
        drafted += 1
    db.flush()
    return {"reminders_drafted": drafted, "escalated": escalated}


def record_payment(db: Session, customer_id: int, amount: float, paid_on: date, mode: str = "", ref: str = "",
                   invoice_id: int | None = None) -> Payment:
    """Record a receipt, applying it to the named invoice or oldest open invoices first."""
    pay = Payment(customer_id=customer_id, invoice_id=invoice_id, paid_on=paid_on, amount=amount, mode=mode, ref=ref)
    db.add(pay)
    remaining = amount
    invs = [db.get(Invoice, invoice_id)] if invoice_id else open_invoices(db, customer_id)
    for inv in invs:
        if remaining <= 0 or inv is None:
            break
        applied = min(inv.outstanding, remaining)
        inv.paid_amount = round(inv.paid_amount + applied, 2)
        remaining -= applied
    db.flush()
    return pay
