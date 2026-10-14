"""Quote pricing (module 2).

Delivered rate per MT (before GST) =
    today's ex-works base rate
  + variant/grade premium
  + payment-term adder (credit costs money)
  + freight to destination
  - approved discount

Nothing here invents a number: if today's rate or the freight for a city is
missing, pricing fails with a PricingError and the agent hands over to a person.
"""

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Customer, DailyRate, FreightRate, PaymentTerm, Product, ProductVariant, Quote, QuoteLine
from app.util import next_number, today

EX_WORKS = {"ex-works", "ex works", "exworks", "siltara", "plant", "self pickup", "pickup"}


class PricingError(ValueError):
    """Pricing can't be completed automatically; the message is safe to show staff."""


@dataclass
class LineCalc:
    product: Product
    variant: str
    qty_mt: float
    base_rate: float
    premium: float
    payment_adder: float
    freight_per_mt: float
    discount_per_mt: float

    @property
    def rate_per_mt(self) -> float:
        return round(self.base_rate + self.premium + self.payment_adder + self.freight_per_mt - self.discount_per_mt, 2)

    @property
    def amount(self) -> float:
        return round(self.rate_per_mt * self.qty_mt, 2)

    @property
    def gst(self) -> float:
        return round(self.amount * self.product.gst_rate, 2)


@dataclass
class QuoteCalc:
    destination: str
    payment_term: PaymentTerm
    lines: list[LineCalc]
    approval_reasons: list[str] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    @property
    def gst_total(self) -> float:
        return round(sum(line.gst for line in self.lines), 2)

    @property
    def grand_total(self) -> float:
        return round(self.subtotal + self.gst_total, 2)

    @property
    def needs_approval(self) -> bool:
        return bool(self.approval_reasons)


def find_product(db: Session, text: str) -> Product | None:
    """Match by code, exact name, or a loose name/category match."""
    t = (text or "").strip().lower()
    if not t:
        return None
    products = db.scalars(select(Product)).all()
    for p in products:
        if t in (p.code.lower(), p.name.lower()):
            return p
    aliases = {
        "dri": "sponge_iron", "sponge": "sponge_iron", "billet": "billet", "angle": "section",
        "channel": "section", "beam": "section", "joist": "section", "section": "section",
        "shot": "shots_grits", "grit": "shots_grits",
    }
    for key, cat in aliases.items():
        if key in t:
            matches = [p for p in products if p.category == cat]
            # Prefer a product whose name shares a word with the request.
            for p in matches:
                if any(w in t for w in p.name.lower().split() if len(w) > 3):
                    return p
            if matches:
                return matches[0]
    for p in products:
        if p.name.lower() in t or t in p.name.lower():
            return p
    return None


def find_variant(product: Product, text: str) -> ProductVariant | None:
    t = (text or "").strip().lower()
    if not t:
        return None
    for v in product.variants:
        if v.name.lower() == t:
            return v
    for v in product.variants:
        if t in v.name.lower() or v.name.lower() in t:
            return v
    return None


def todays_rate(db: Session, product: Product, on: date | None = None) -> DailyRate | None:
    return db.scalar(
        select(DailyRate).where(DailyRate.product_id == product.id, DailyRate.rate_date == (on or today()))
    )


def freight_for(db: Session, destination: str) -> float:
    d = (destination or "").strip().lower()
    if not d:
        raise PricingError("Delivery location is needed to work out freight.")
    if d in EX_WORKS:
        return 0.0
    row = db.scalar(select(FreightRate).where(func.lower(FreightRate.city) == d))
    if row is None:
        raise PricingError(f"No freight rate on file for '{destination}'. Sales team must quote freight manually.")
    return row.per_mt


def price_quote(
    db: Session,
    items: list[dict],
    destination: str,
    payment_term_code: str = "advance",
    discount_per_mt: float = 0.0,
    customer: Customer | None = None,
    on: date | None = None,
) -> QuoteCalc:
    """items: [{"product": "sponge iron", "variant": "lumps", "qty_mt": 50}, ...]"""
    if not items:
        raise PricingError("No items to quote.")
    term = db.get(PaymentTerm, payment_term_code or "advance")
    if term is None:
        codes = ", ".join(t.code for t in db.scalars(select(PaymentTerm)))
        raise PricingError(f"Unknown payment term '{payment_term_code}'. Known terms: {codes}.")
    freight = freight_for(db, destination)

    reasons: list[str] = []
    lines: list[LineCalc] = []
    for item in items:
        product = find_product(db, str(item.get("product", "")))
        if product is None:
            raise PricingError(f"Product '{item.get('product')}' is not in the catalogue.")
        if product.status != "live":
            raise PricingError(f"{product.name} is not in regular production yet; the sales team will follow up.")
        qty = float(item.get("qty_mt") or 0)
        if qty <= 0:
            raise PricingError(f"Quantity for {product.name} is missing.")
        if product.min_order_mt and qty < product.min_order_mt:
            raise PricingError(f"Minimum order for {product.name} is {product.min_order_mt:g} MT.")
        rate = todays_rate(db, product, on)
        if rate is None:
            raise PricingError(f"Today's rate for {product.name} has not been entered yet.")
        variant = find_variant(product, str(item.get("variant") or ""))
        if item.get("variant") and variant is None:
            names = ", ".join(v.name for v in product.variants) or "none"
            raise PricingError(f"Unknown size/grade '{item.get('variant')}' for {product.name}. Available: {names}.")
        lines.append(
            LineCalc(
                product=product,
                variant=variant.name if variant else "",
                qty_mt=qty,
                base_rate=rate.base_rate_per_mt,
                premium=variant.premium_per_mt if variant else 0.0,
                payment_adder=term.adder_per_mt,
                freight_per_mt=freight,
                discount_per_mt=max(discount_per_mt, 0.0),
            )
        )

    calc = QuoteCalc(destination=destination, payment_term=term, lines=lines, approval_reasons=reasons)
    if discount_per_mt > 0:
        reasons.append(f"discount of ₹{discount_per_mt:g}/MT requested")
    if calc.subtotal > settings.quote_auto_limit:
        reasons.append(f"value above auto-approval limit of ₹{settings.quote_auto_limit:,.0f}")
    if term.days > 0:
        if customer is None:
            reasons.append("credit terms for a new party")
        elif term.days > customer.credit_days:
            reasons.append(f"{term.days}-day credit exceeds customer's approved {customer.credit_days} days")
    return calc


def save_quote(
    db: Session,
    calc: QuoteCalc,
    party_name: str,
    party_phone: str = "",
    lead_id: int | None = None,
    customer_id: int | None = None,
    notes: str = "",
) -> Quote:
    quote = Quote(
        number=next_number(db, "quote", "QT"),
        lead_id=lead_id,
        customer_id=customer_id,
        party_name=party_name,
        party_phone=party_phone,
        status="pending_approval" if calc.needs_approval else "approved",
        approved_by="" if calc.needs_approval else "auto",
        approval_reason="; ".join(calc.approval_reasons),
        valid_until=today(),
        destination=calc.destination,
        payment_term_code=calc.payment_term.code,
        subtotal=calc.subtotal,
        gst_total=calc.gst_total,
        grand_total=calc.grand_total,
        notes=notes,
    )
    for line in calc.lines:
        quote.lines.append(
            QuoteLine(
                product_id=line.product.id,
                variant=line.variant,
                qty_mt=line.qty_mt,
                base_rate=line.base_rate,
                premium=line.premium,
                payment_adder=line.payment_adder,
                freight_per_mt=line.freight_per_mt,
                discount_per_mt=line.discount_per_mt,
                rate_per_mt=line.rate_per_mt,
                gst_rate=line.product.gst_rate,
                amount=line.amount,
            )
        )
    db.add(quote)
    db.flush()
    return quote
