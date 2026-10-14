"""Order and dispatch tracking (module 4)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Order, Quote
from app.util import next_number, today

STATUS_TEXT = {
    "confirmed": "Order confirmed, scheduled for production",
    "in_production": "In production",
    "ready": "Material ready, awaiting loading",
    "dispatched": "Dispatched",
    "delivered": "Delivered",
    "cancelled": "Cancelled",
}


def order_summary(order: Order) -> dict:
    out = {
        "order_no": order.number,
        "date": order.order_date.isoformat(),
        "item": order.item_text or (order.product.name if order.product else ""),
        "qty_mt": order.qty_mt,
        "status": STATUS_TEXT.get(order.status, order.status),
        "destination": order.destination,
        "dispatches": [],
    }
    for d in order.dispatches:
        out["dispatches"].append({
            "dispatched_at": d.dispatched_at.strftime("%d-%b %H:%M"),
            "truck_no": d.truck_no,
            "driver_phone": d.driver_phone,
            "lr_no": d.lr_no,
            "qty_mt": d.qty_mt,
            "eta": d.eta.isoformat() if d.eta else None,
            "delivered": bool(d.delivered_at),
            "test_certificate_available": bool(d.mtc_path),
        })
    return out


def recent_orders(db: Session, customer_id: int, limit: int = 5) -> list[Order]:
    return list(
        db.scalars(
            select(Order).where(Order.customer_id == customer_id).order_by(Order.order_date.desc(), Order.id.desc()).limit(limit)
        )
    )


def order_from_quote(db: Session, quote: Quote, customer_id: int) -> list[Order]:
    """Convert an accepted quote to one order per line."""
    orders = []
    for line in quote.lines:
        o = Order(
            number=next_number(db, "order", "SO"),
            customer_id=customer_id,
            quote_id=quote.id,
            order_date=today(),
            product_id=line.product_id,
            item_text=f"{line.product.name} {line.variant}".strip(),
            qty_mt=line.qty_mt,
            rate_per_mt=line.rate_per_mt,
            amount=line.amount,
            destination=quote.destination,
        )
        db.add(o)
        orders.append(o)
    quote.status = "accepted"
    db.flush()
    return orders
