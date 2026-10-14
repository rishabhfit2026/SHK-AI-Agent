"""Daily management brief (module 6)."""

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import llm
from app.agent.prompts import BRIEF_SYSTEM
from app.config import settings
from app.models import Brief, DailyRate, Lead, MarketPrice, Order, Outbox, Product, Quote, Task
from app.services import pipeline, receivables, reorder
from app.util import inr, today

log = logging.getLogger(__name__)


def _lakh(x: float) -> str:
    if abs(x) >= 1e7:
        return f"₹{x / 1e7:.2f} Cr"
    if abs(x) >= 1e5:
        return f"₹{x / 1e5:.2f} L"
    return inr(x)


def compute_metrics(db: Session, on: date | None = None) -> dict:
    on = on or today()
    start = datetime.combine(on - timedelta(days=1), datetime.min.time())
    end = datetime.combine(on, datetime.min.time())

    leads = db.scalars(select(Lead).where(Lead.created_at >= start, Lead.created_at < end)).all()
    by_source: dict[str, int] = {}
    for lead in leads:
        by_source[lead.source] = by_source.get(lead.source, 0) + 1
    hot = db.scalars(
        select(Lead).where(Lead.status.in_(("new", "qualified", "quoted")), Lead.score >= 60).order_by(Lead.score.desc()).limit(5)
    ).all()

    quotes = db.scalars(select(Quote).where(Quote.created_at >= start, Quote.created_at < end)).all()
    pending = db.scalars(select(Quote).where(Quote.status == "pending_approval")).all()
    orders = db.scalars(select(Order).where(Order.order_date == on - timedelta(days=1))).all()

    ageing = receivables.ageing(db, on)
    total_out = sum(r.total for r in ageing)
    overdue = sum(r.overdue for r in ageing)
    over_90 = sum(r.buckets["90+"] for r in ageing)

    cycles = [c for c in reorder.all_cycles(db, on) if c.lapsed]
    cycles.sort(key=lambda c: -(c.days_since or 0))

    overdue_tasks = db.scalars(select(Task).where(Task.status == "open", Task.due_date < on)).all()
    by_rep: dict[str, int] = {}
    for t in overdue_tasks:
        name = t.rep.name if t.rep else "unassigned"
        by_rep[name] = by_rep.get(name, 0) + 1

    live = db.scalars(select(Product).where(Product.status == "live")).all()
    rated = set(db.scalars(select(DailyRate.product_id).where(DailyRate.rate_date == on)))
    missing_rates = [p.name for p in live if p.id not in rated]

    market = []
    items = set(db.scalars(select(MarketPrice.item)))
    for item in sorted(items):
        pts = db.scalars(
            select(MarketPrice).where(MarketPrice.item == item, MarketPrice.price_date <= on)
            .order_by(MarketPrice.price_date.desc()).limit(2)
        ).all()
        if not pts:
            continue
        row = {"item": item, "latest": pts[0].price, "unit": pts[0].unit, "date": str(pts[0].price_date)}
        if len(pts) == 2 and pts[1].price:
            row["change_pct"] = round((pts[0].price - pts[1].price) / pts[1].price * 100, 1)
        market.append(row)

    return {
        "date": str(on),
        "leads_yesterday": {"count": len(leads), "by_source": by_source},
        "hot_leads": [
            {"who": lead.company or lead.name or lead.phone, "product": lead.product.name if lead.product else "",
             "qty_mt": lead.qty_mt, "score": lead.score, "status": lead.status} for lead in hot
        ],
        "quotes_yesterday": {"count": len(quotes), "value": _lakh(sum(q.subtotal for q in quotes)),
                             "accepted": sum(1 for q in quotes if q.status == "accepted")},
        "quotes_pending_approval": [{"number": q.number, "party": q.party_name, "value": _lakh(q.subtotal)} for q in pending],
        "orders_yesterday": {"count": len(orders), "qty_mt": round(sum(o.qty_mt for o in orders), 1),
                             "value": _lakh(sum(o.amount for o in orders))},
        "receivables": {"total": _lakh(total_out), "overdue": _lakh(overdue), "over_90_days": _lakh(over_90),
                        "top_overdue": [{"customer": r.customer.name, "overdue": _lakh(r.overdue)} for r in ageing[:3] if r.overdue]},
        "lapsed_customers": [{"customer": c.customer.name, "days_since": c.days_since, "usual_gap": c.usual_gap_days}
                             for c in cycles[:5]],
        "lapsed_count": len(cycles),
        "overdue_tasks_by_rep": by_rep,
        "pipeline": {"funnel": pipeline.funnel(db), "actions_due": len(pipeline.due_actions(db, on))},
        "rates_not_entered_today": missing_rates,
        "market_prices": market,
    }


def fallback_narrative(m: dict) -> str:
    lines = [f"SHK Sales Brief {m['date']}"]
    r = m["receivables"]
    lines.append(f"• Outstanding {r['total']}, overdue {r['overdue']}, 90+ days {r['over_90_days']}")
    lines.append(f"• Leads yesterday: {m['leads_yesterday']['count']} {m['leads_yesterday']['by_source'] or ''}")
    o = m["orders_yesterday"]
    lines.append(f"• Orders yesterday: {o['count']} ({o['qty_mt']} MT, {o['value']})")
    q = m["quotes_yesterday"]
    lines.append(f"• Quotes yesterday: {q['count']} worth {q['value']}; awaiting approval: {len(m['quotes_pending_approval'])}")
    if m["lapsed_count"]:
        names = ", ".join(c["customer"] for c in m["lapsed_customers"])
        lines.append(f"• {m['lapsed_count']} customers past their usual order cycle: {names}")
    if m["rates_not_entered_today"]:
        lines.append(f"• Today's rate not entered: {', '.join(m['rates_not_entered_today'])} (AI can't quote these)")
    lines.append(f"• New-product pipeline actions due: {m['pipeline']['actions_due']}")
    return "\n".join(lines)


def generate_brief(db: Session, on: date | None = None, client=None, send: bool = True) -> Brief:
    on = on or today()
    metrics = compute_metrics(db, on)
    try:
        narrative = llm.complete_text(BRIEF_SYSTEM, json.dumps(metrics, ensure_ascii=False), effort="low", client=client)
    except llm.LLMUnavailable as e:
        log.info("brief without LLM: %s", e)
        narrative = fallback_narrative(metrics)
    narrative = narrative or fallback_narrative(metrics)

    brief = db.scalar(select(Brief).where(Brief.brief_date == on))
    if brief is None:
        brief = Brief(brief_date=on)
        db.add(brief)
    brief.metrics, brief.narrative = metrics, narrative
    if send:
        for email in settings.management_emails:
            key = f"brief:{on}:{email}"
            if not db.scalar(select(Outbox.id).where(Outbox.dedupe_key == key)):
                # Internal email to directors: pre-approved.
                db.add(Outbox(to_email=email, to_name="Management", subject=f"SHK sales brief {on:%d %b %Y}",
                              body=narrative, kind="brief", status="approved", decided_by="auto", dedupe_key=key))
    db.flush()
    return brief
