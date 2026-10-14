"""Lead capture, scoring and routing (module 1)."""

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Contact, Customer, Lead, SalesRep, Task
from app.services.pricing import find_product
from app.util import log_activity, norm_phone, now

# Where freight makes SHK competitive today. Leads elsewhere still get answered, just score lower.
CORE_STATES = {"chhattisgarh", "maharashtra", "madhya pradesh", "odisha", "telangana", "jharkhand"}
OPEN_STATUSES = ("new", "qualified", "quoted", "nurturing")

LEAD_FIELDS = (
    "name", "company", "email", "city", "state", "variant_text", "qty_mt",
    "frequency", "payment_terms", "timeline", "requirement",
)


def customer_for_phone(db: Session, phone: str) -> Customer | None:
    p = norm_phone(phone)
    if not p:
        return None
    cust = db.scalar(select(Customer).where(Customer.phone == p))
    if cust:
        return cust
    contact = db.scalar(select(Contact).where(Contact.phone == p))
    return contact.customer if contact else None


def open_lead_for_phone(db: Session, phone: str) -> Lead | None:
    p = norm_phone(phone)
    if not p:
        return None
    return db.scalar(
        select(Lead).where(Lead.phone == p, Lead.status.in_(OPEN_STATUSES)).order_by(Lead.id.desc())
    )


def score_lead(lead: Lead) -> int:
    score = 0
    if lead.product_id:
        score += 15
    if lead.qty_mt:
        score += 15
        if lead.qty_mt >= 100:
            score += 15
        elif lead.qty_mt >= 25:
            score += 8
    if (lead.state or "").strip().lower() in CORE_STATES:
        score += 10
    freq = (lead.frequency or "").lower()
    if any(w in freq for w in ("week", "month", "regular", "daily", "recurring")):
        score += 15
    if "advance" in (lead.payment_terms or "").lower():
        score += 10
    tl = (lead.timeline or "").lower()
    if any(w in tl for w in ("immediate", "urgent", "today", "this week", "asap", "tomorrow")):
        score += 10
    if lead.customer_id:
        score += 10
    if lead.company:
        score += 5
    return min(score, 100)


def assign_rep(db: Session, lead: Lead) -> SalesRep | None:
    reps = db.scalars(select(SalesRep).where(SalesRep.active.is_(True))).all()
    if not reps:
        return None
    if lead.customer and lead.customer.rep_id:
        return db.get(SalesRep, lead.customer.rep_id)
    state = (lead.state or "").strip().lower()
    category = lead.product.category if lead.product else ""

    def covers(rep: SalesRep) -> bool:
        states = {s.strip().lower() for s in rep.states.split(",") if s.strip()}
        cats = {c.strip() for c in rep.categories.split(",") if c.strip()}
        return (not states or state in states) and (not cats or not category or category in cats)

    candidates = [r for r in reps if covers(r)] or reps
    load = dict(
        db.execute(
            select(Lead.rep_id, func.count()).where(Lead.status.in_(OPEN_STATUSES)).group_by(Lead.rep_id)
        ).all()
    )
    return min(candidates, key=lambda r: (load.get(r.id, 0), r.id))


def upsert_lead(db: Session, phone: str, source: str, fields: dict, lead: Lead | None = None) -> Lead:
    """Create or update the open lead for this phone with whatever details we now know."""
    phone = norm_phone(phone)
    if lead is None:
        lead = open_lead_for_phone(db, phone) if phone else None
    is_new = lead is None
    if is_new:
        lead = Lead(source=source, phone=phone)
        db.add(lead)
        customer = customer_for_phone(db, phone)
        if customer:
            lead.customer_id = customer.id
            lead.customer = customer
            lead.company = customer.name
            lead.city, lead.state = customer.city, customer.state

    for key in LEAD_FIELDS:
        value = fields.get(key)
        if value in (None, ""):
            continue
        if key == "qty_mt":
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
        setattr(lead, key, value)
    if fields.get("product"):
        product = find_product(db, str(fields["product"]))
        if product:
            lead.product_id = product.id
            lead.product = product

    if lead.status == "new" and lead.product_id and lead.qty_mt and (lead.city or lead.state):
        lead.status = "qualified"
    lead.score = score_lead(lead)
    db.flush()

    if is_new or lead.rep_id is None:
        rep = assign_rep(db, lead)
        if rep:
            lead.rep_id = rep.id
    if is_new:
        lead.next_followup_at = now() + timedelta(hours=4)
        log_activity(db, "lead", lead.id, "created", f"Lead captured via {source}")
    db.flush()
    return lead


def create_task(db: Session, kind: str, title: str, details: str = "", dedupe_key: str | None = None, **links) -> Task | None:
    """Create a task unless one with the same dedupe_key already exists."""
    if dedupe_key and db.scalar(select(Task.id).where(Task.dedupe_key == dedupe_key)):
        return None
    task = Task(kind=kind, title=title, details=details, dedupe_key=dedupe_key, **links)
    db.add(task)
    db.flush()
    return task


def search_customers(db: Session, q: str, limit: int = 10) -> list[Customer]:
    like = f"%{q.strip()}%"
    return list(
        db.scalars(
            select(Customer)
            .where(or_(Customer.name.ilike(like), Customer.city.ilike(like), Customer.phone.ilike(like)))
            .limit(limit)
        )
    )
