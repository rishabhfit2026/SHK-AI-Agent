"""Tools for the internal staff assistant ("Ask" page in the dashboard)."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Lead, MarketPrice, Order, Outbox, Product, Prospect, Quote, Task
from app.services import crm, pipeline, receivables, reorder
from app.services.orders import order_summary, recent_orders
from app.util import inr, today


@dataclass
class StaffContext:
    db: Session
    user: str


def _lead_row(lead: Lead) -> dict:
    return {
        "id": lead.id, "created": lead.created_at.strftime("%d-%b"), "source": lead.source, "status": lead.status,
        "score": lead.score, "company": lead.company or lead.name, "phone": lead.phone,
        "product": lead.product.name if lead.product else "", "qty_mt": lead.qty_mt,
        "location": ", ".join(x for x in (lead.city, lead.state) if x), "rep": lead.rep.name if lead.rep else "",
    }


def list_leads(ctx: StaffContext, status: str = "", min_score: int = 0, days: int = 30, limit: int = 20) -> dict:
    q = select(Lead).where(Lead.created_at >= today() - timedelta(days=days), Lead.score >= min_score)
    if status:
        q = q.where(Lead.status == status)
    leads = ctx.db.scalars(q.order_by(Lead.score.desc(), Lead.id.desc()).limit(limit)).all()
    return {"leads": [_lead_row(lead) for lead in leads]}


def customer_overview(ctx: StaffContext, name: str) -> dict:
    matches = crm.search_customers(ctx.db, name, limit=3)
    if not matches:
        return {"error": f"No customer matching '{name}'."}
    out = []
    cycles = {c.customer.id: c for c in reorder.all_cycles(ctx.db)}
    for cust in matches:
        cyc = cycles.get(cust.id)
        open_tasks = ctx.db.scalars(select(Task).where(Task.customer_id == cust.id, Task.status == "open")).all()
        out.append({
            "name": cust.name, "city": cust.city, "state": cust.state, "segment": cust.segment,
            "phone": cust.phone, "rep": cust.rep.name if cust.rep else "", "credit_days": cust.credit_days,
            "orders_total": cyc.orders if cyc else 0,
            "usual_gap_days": cyc.usual_gap_days if cyc else None,
            "days_since_last_order": cyc.days_since if cyc else None,
            "lapsed": cyc.lapsed if cyc else False,
            "recent_orders": [order_summary(o) for o in recent_orders(ctx.db, cust.id, 3)],
            "outstanding": receivables.statement_text(ctx.db, cust),
            "open_tasks": [t.title for t in open_tasks],
        })
    return {"customers": out}


def reorder_watchlist(ctx: StaffContext) -> dict:
    rows = []
    for c in reorder.all_cycles(ctx.db):
        if c.lapsed or c.volume_drop or c.due_soon:
            rows.append({
                "customer": c.customer.name, "usual_gap_days": c.usual_gap_days, "days_since": c.days_since,
                "status": "lapsed" if c.lapsed else ("volume drop" if c.volume_drop else "due now"),
                "avg_qty_mt": c.avg_qty_mt, "last_qty_mt": c.last_qty_mt,
            })
    rows.sort(key=lambda r: (r["status"] != "lapsed", -(r["days_since"] or 0)))
    return {"watchlist": rows}


def receivables_summary(ctx: StaffContext, top: int = 10) -> dict:
    rows = receivables.ageing(ctx.db)
    totals = {b: round(sum(r.buckets[b] for r in rows), 2) for b in receivables.BUCKETS}
    return {
        "totals": {k: inr(v) for k, v in totals.items()},
        "grand_total": inr(sum(totals.values())),
        "top_overdue": [
            {"customer": r.customer.name, "overdue": inr(r.overdue), "total": inr(r.total),
             **{b: inr(r.buckets[b]) for b in receivables.BUCKETS if r.buckets[b]}}
            for r in rows[:top] if r.overdue > 0
        ],
    }


def pipeline_summary(ctx: StaffContext) -> dict:
    prospects = ctx.db.scalars(select(Prospect).order_by(Prospect.next_action_date)).all()
    return {
        "funnel": pipeline.funnel(ctx.db),
        "actions_due": [
            {"company": p.company, "segment": p.segment, "product": p.product.name if p.product else "",
             "stage": p.stage, "next_action": p.next_action, "due": str(p.next_action_date)}
            for p in prospects if p.stage != "lost" and p.next_action_date and p.next_action_date <= today()
        ],
    }


def quotes_status(ctx: StaffContext, days: int = 7) -> dict:
    since = today() - timedelta(days=days)
    quotes = ctx.db.scalars(select(Quote).where(Quote.created_at >= since).order_by(Quote.id.desc())).all()
    by_status: dict[str, int] = {}
    for q in quotes:
        by_status[q.status] = by_status.get(q.status, 0) + 1
    return {
        "counts": by_status,
        "pending_approval": [
            {"number": q.number, "party": q.party_name, "subtotal": inr(q.subtotal), "reason": q.approval_reason}
            for q in quotes if q.status == "pending_approval"
        ],
    }


def sales_summary(ctx: StaffContext, days: int = 30) -> dict:
    since = today() - timedelta(days=days)
    rows = ctx.db.execute(
        select(Product.name, func.count(Order.id), func.sum(Order.qty_mt), func.sum(Order.amount))
        .join(Product, Order.product_id == Product.id)
        .where(Order.order_date >= since, Order.status != "cancelled")
        .group_by(Product.name)
    ).all()
    return {
        "period_days": days,
        "by_product": [{"product": n, "orders": c, "qty_mt": round(q or 0, 1), "value": inr(a or 0)} for n, c, q, a in rows],
    }


def market_prices(ctx: StaffContext, days: int = 14) -> dict:
    since = today() - timedelta(days=days)
    rows = ctx.db.scalars(
        select(MarketPrice).where(MarketPrice.price_date >= since).order_by(MarketPrice.item, MarketPrice.price_date)
    ).all()
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r.item, []).append({"date": str(r.price_date), "price": r.price, "unit": r.unit})
    return {"series": series}


def draft_message(ctx: StaffContext, body: str, customer_name: str = "", to_email: str = "",
                  kind: str = "outreach") -> dict:
    cust = crm.search_customers(ctx.db, customer_name, limit=1)[0] if customer_name else None
    if cust is None and not to_email:
        return {"error": "Give a customer name that exists in the CRM, or an email address."}
    ob = Outbox(customer_id=cust.id if cust else None, to_name=cust.name if cust else to_email,
                to_phone=cust.phone if cust else "", to_email=to_email, body=body, kind=kind)
    ctx.db.add(ob)
    ctx.db.flush()
    return {"ok": True, "outbox_id": ob.id, "note": "Waiting for approval in the Outbox."}


def _int(desc: str) -> dict:
    return {"type": "integer", "description": desc}


TOOLS = [
    {"name": "list_leads", "description": "Recent leads, highest score first.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["", "new", "qualified", "quoted", "won", "lost", "nurturing"]},
         "min_score": _int("0-100"), "days": _int("look-back window"), "limit": _int("max rows")}}},
    {"name": "customer_overview", "description": "Everything about a customer: buying cycle, recent orders, dues, open tasks.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "reorder_watchlist", "description": "Customers who are overdue for their usual order, due now, or buying less.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "receivables_summary", "description": "Outstanding by ageing bucket and the top overdue customers.",
     "input_schema": {"type": "object", "properties": {"top": _int("rows")}}},
    {"name": "pipeline_summary", "description": "New-product (grinding media, tooth points, castings) funnel and due actions.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "quotes_status", "description": "Quotes by status and those waiting for approval.",
     "input_schema": {"type": "object", "properties": {"days": _int("look-back window")}}},
    {"name": "sales_summary", "description": "Orders, MT and value by product over a period.",
     "input_schema": {"type": "object", "properties": {"days": _int("look-back window")}}},
    {"name": "market_prices", "description": "Market price entries (sponge iron, billet, ore, coal...) recorded by staff.",
     "input_schema": {"type": "object", "properties": {"days": _int("look-back window")}}},
    {"name": "draft_message",
     "description": ("Draft a message to a buyer into the Outbox for approval. Never sends directly. On approval it "
                     "goes to the customer's portal inbox and by email."),
     "input_schema": {"type": "object", "properties": {
         "customer_name": {"type": "string", "description": "Existing customer to message"},
         "to_email": {"type": "string", "description": "Email, for someone who isn't a customer yet"},
         "body": {"type": "string"},
         "kind": {"type": "string", "enum": ["outreach", "reorder", "payment_reminder", "quote"]}},
         "required": ["body"]}},
]

HANDLERS = {
    "list_leads": list_leads, "customer_overview": customer_overview, "reorder_watchlist": reorder_watchlist,
    "receivables_summary": receivables_summary, "pipeline_summary": pipeline_summary,
    "quotes_status": quotes_status, "sales_summary": sales_summary, "market_prices": market_prices,
    "draft_message": draft_message,
}
