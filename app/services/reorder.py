"""Customer buying-cycle tracking and reorder nudges (module 3)."""

from dataclasses import dataclass
from datetime import date
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Customer, Order, Outbox
from app.services.crm import create_task
from app.util import today

MIN_ORDERS = 3  # need at least this many orders to trust a cycle


@dataclass
class CycleStatus:
    customer: Customer
    orders: int
    usual_gap_days: float | None
    last_order: date | None
    days_since: int | None
    avg_qty_mt: float
    last_qty_mt: float

    @property
    def lapsed(self) -> bool:
        return (
            self.usual_gap_days is not None
            and self.days_since is not None
            and self.days_since > self.usual_gap_days * settings.reorder_lapse_factor
        )

    @property
    def due_soon(self) -> bool:
        return (
            not self.lapsed
            and self.usual_gap_days is not None
            and self.days_since is not None
            and self.days_since >= self.usual_gap_days - 1
        )

    @property
    def volume_drop(self) -> bool:
        return self.orders >= MIN_ORDERS and self.avg_qty_mt > 0 and self.last_qty_mt < 0.6 * self.avg_qty_mt


def cycle_for(customer: Customer, orders: list[Order], on: date) -> CycleStatus:
    dates = sorted({o.order_date for o in orders})
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    qtys = [o.qty_mt for o in sorted(orders, key=lambda o: o.order_date)]
    return CycleStatus(
        customer=customer,
        orders=len(orders),
        usual_gap_days=float(median(gaps)) if len(dates) >= MIN_ORDERS else None,
        last_order=dates[-1] if dates else None,
        days_since=(on - dates[-1]).days if dates else None,
        avg_qty_mt=round(sum(qtys) / len(qtys), 2) if qtys else 0.0,
        last_qty_mt=qtys[-1] if qtys else 0.0,
    )


def all_cycles(db: Session, on: date | None = None) -> list[CycleStatus]:
    on = on or today()
    by_customer: dict[int, list[Order]] = {}
    for o in db.scalars(select(Order).where(Order.status != "cancelled")):
        by_customer.setdefault(o.customer_id, []).append(o)
    out = []
    for cust in db.scalars(select(Customer)):
        out.append(cycle_for(cust, by_customer.get(cust.id, []), on))
    return out


def run_reorder_scan(db: Session, on: date | None = None) -> dict:
    """Daily job: task for the rep on every lapsed customer, plus a draft check-in message for approval."""
    on = on or today()
    created_tasks = drafted = 0
    for c in all_cycles(db, on):
        if not (c.lapsed or c.volume_drop):
            continue
        why = (
            f"usually orders every {c.usual_gap_days:g} days, last order {c.days_since} days ago"
            if c.lapsed
            else f"last order {c.last_qty_mt:g} MT vs usual {c.avg_qty_mt:g} MT"
        )
        # One nudge per customer per last-order date, so the scan is safe to run daily.
        key = f"reorder:{c.customer.id}:{c.last_order}"
        task = create_task(
            db,
            kind="reorder_nudge",
            title=f"Call {c.customer.name}: {why}",
            details="Check if they moved to another supplier, have a plant shutdown, or need a revised rate.",
            dedupe_key=key,
            customer_id=c.customer.id,
            rep_id=c.customer.rep_id,
            due_date=on,
        )
        if task:
            created_tasks += 1
        if c.lapsed and not db.scalar(select(Outbox.id).where(Outbox.dedupe_key == key)):
            db.add(Outbox(
                to_phone=c.customer.phone,
                to_name=c.customer.name,
                kind="reorder",
                customer_id=c.customer.id,
                dedupe_key=key,
                body=(
                    f"Namaste from Shri Hare-Krishna Sponge Iron. We haven't received your order in a while "
                    f"and wanted to check if you need material this week. Ask in the chat on your customer portal "
                    f"for today's rate delivered to {c.customer.city or 'your site'}, or reply to this email."
                ),
            ))
            drafted += 1
    db.flush()
    return {"tasks_created": created_tasks, "messages_drafted": drafted}
