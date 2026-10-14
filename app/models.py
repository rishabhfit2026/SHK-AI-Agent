"""Database tables for the SHK Sales Desk.

Amounts are INR, quantities are metric tonnes (MT). Phone numbers are stored
as digits only with country code (e.g. 919826420222), see app.util.norm_phone.
"""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.util import now


class SalesRep(Base):
    __tablename__ = "sales_reps"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    # Comma-separated state names this rep covers; used for lead routing.
    states: Mapped[str] = mapped_column(String(300), default="")
    # Comma-separated product categories this rep owns (blank = all).
    categories: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    gstin: Mapped[str] = mapped_column(String(20), default="")
    phone: Mapped[str] = mapped_column(String(20), default="", index=True)
    email: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(100), default="")
    segment: Mapped[str] = mapped_column(String(60), default="")  # induction furnace, rolling mill, foundry, trader...
    credit_days: Mapped[int] = mapped_column(Integer, default=0)
    credit_limit: Mapped[float] = mapped_column(Float, default=0)
    rep_id: Mapped[int | None] = mapped_column(ForeignKey("sales_reps.id"))
    source: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    rep: Mapped[SalesRep | None] = relationship()
    contacts: Mapped[list["Contact"]] = relationship(back_populates="customer", cascade="all, delete-orphan")


class Contact(Base):
    """Extra people and numbers at a customer."""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(20), index=True)
    role: Mapped[str] = mapped_column(String(60), default="")
    customer: Mapped[Customer] = relationship(back_populates="contacts")


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    # sponge_iron | billet | section | shots_grits | new_product
    category: Mapped[str] = mapped_column(String(40))
    hsn: Mapped[str] = mapped_column(String(12), default="")
    gst_rate: Mapped[float] = mapped_column(Float, default=0.18)
    spec: Mapped[str] = mapped_column(Text, default="")
    min_order_mt: Mapped[float] = mapped_column(Float, default=0)
    # live = sold today; upcoming = FY26-27 expansion (pipeline only, no quotes)
    status: Mapped[str] = mapped_column(String(20), default="live")
    variants: Mapped[list["ProductVariant"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class ProductVariant(Base):
    __tablename__ = "product_variants"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    name: Mapped[str] = mapped_column(String(120))
    premium_per_mt: Mapped[float] = mapped_column(Float, default=0)
    product: Mapped[Product] = relationship(back_populates="variants")


class DailyRate(Base):
    """Ex-works base price per MT, entered each morning by the sales head."""

    __tablename__ = "daily_rates"
    __table_args__ = (UniqueConstraint("product_id", "rate_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    base_rate_per_mt: Mapped[float] = mapped_column(Float)
    entered_by: Mapped[str] = mapped_column(String(80), default="")
    product: Mapped[Product] = relationship()


class FreightRate(Base):
    __tablename__ = "freight_rates"
    id: Mapped[int] = mapped_column(primary_key=True)
    city: Mapped[str] = mapped_column(String(100), unique=True)  # lower-case
    state: Mapped[str] = mapped_column(String(100), default="")
    per_mt: Mapped[float] = mapped_column(Float)


class PaymentTerm(Base):
    __tablename__ = "payment_terms"
    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    days: Mapped[int] = mapped_column(Integer, default=0)
    adder_per_mt: Mapped[float] = mapped_column(Float, default=0)


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    source: Mapped[str] = mapped_column(String(30))  # web | portal | manual
    external_id: Mapped[str] = mapped_column(String(60), default="", index=True)
    # new | qualified | quoted | won | lost | nurturing
    status: Mapped[str] = mapped_column(String(20), default="new")
    name: Mapped[str] = mapped_column(String(120), default="")
    company: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(20), default="", index=True)
    email: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(100), default="")
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    variant_text: Mapped[str] = mapped_column(String(120), default="")
    qty_mt: Mapped[float | None] = mapped_column(Float)
    frequency: Mapped[str] = mapped_column(String(40), default="")  # one-time | weekly | monthly
    payment_terms: Mapped[str] = mapped_column(String(40), default="")
    timeline: Mapped[str] = mapped_column(String(80), default="")
    requirement: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[int] = mapped_column(Integer, default=0)
    rep_id: Mapped[int | None] = mapped_column(ForeignKey("sales_reps.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    next_followup_at: Mapped[datetime | None] = mapped_column(DateTime)
    lost_reason: Mapped[str] = mapped_column(String(200), default="")

    product: Mapped[Product | None] = relationship()
    rep: Mapped[SalesRep | None] = relationship()
    customer: Mapped[Customer | None] = relationship()


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("channel", "external_user"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(20))  # web (anonymous) | portal | staff
    external_user: Mapped[str] = mapped_column(String(80))  # web session id, "user:<id>", or staff username
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime)
    # When true the AI stays silent and a salesperson replies from the dashboard.
    human_takeover: Mapped[bool] = mapped_column(Boolean, default=False)
    # LLM message history (OpenAI-style messages incl. tool calls).
    history: Mapped[list] = mapped_column(JSON, default=list)

    customer: Mapped[Customer | None] = relationship()
    lead: Mapped[Lead | None] = relationship()
    messages: Mapped[list["ChatMessage"]] = relationship(order_by="ChatMessage.id", cascade="all, delete-orphan")


class ChatMessage(Base):
    """Human-readable transcript (what was actually said), separate from the LLM history."""

    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(String(4))  # in | out
    author: Mapped[str] = mapped_column(String(40), default="")  # customer | ai | rep name
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    party_name: Mapped[str] = mapped_column(String(200), default="")
    party_phone: Mapped[str] = mapped_column(String(20), default="")
    # draft | pending_approval | approved | sent | accepted | rejected | expired
    status: Mapped[str] = mapped_column(String(20), default="draft")
    valid_until: Mapped[date] = mapped_column(Date)
    destination: Mapped[str] = mapped_column(String(100), default="")
    payment_term_code: Mapped[str] = mapped_column(String(20), default="advance")
    subtotal: Mapped[float] = mapped_column(Float, default=0)
    gst_total: Mapped[float] = mapped_column(Float, default=0)
    grand_total: Mapped[float] = mapped_column(Float, default=0)
    approval_reason: Mapped[str] = mapped_column(String(200), default="")
    approved_by: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(String(300), default="")

    lines: Mapped[list["QuoteLine"]] = relationship(back_populates="quote", cascade="all, delete-orphan")
    lead: Mapped[Lead | None] = relationship()
    customer: Mapped[Customer | None] = relationship()


class QuoteLine(Base):
    __tablename__ = "quote_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    quote_id: Mapped[int] = mapped_column(ForeignKey("quotes.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    variant: Mapped[str] = mapped_column(String(120), default="")
    qty_mt: Mapped[float] = mapped_column(Float)
    base_rate: Mapped[float] = mapped_column(Float)
    premium: Mapped[float] = mapped_column(Float, default=0)
    payment_adder: Mapped[float] = mapped_column(Float, default=0)
    freight_per_mt: Mapped[float] = mapped_column(Float, default=0)
    discount_per_mt: Mapped[float] = mapped_column(Float, default=0)
    rate_per_mt: Mapped[float] = mapped_column(Float)  # delivered (FOR) rate before GST
    gst_rate: Mapped[float] = mapped_column(Float, default=0.18)
    amount: Mapped[float] = mapped_column(Float)  # before GST

    quote: Mapped[Quote] = relationship(back_populates="lines")
    product: Mapped[Product] = relationship()


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    order_date: Mapped[date] = mapped_column(Date, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    item_text: Mapped[str] = mapped_column(String(200), default="")
    qty_mt: Mapped[float] = mapped_column(Float)
    rate_per_mt: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    destination: Mapped[str] = mapped_column(String(100), default="")
    # confirmed | in_production | ready | dispatched | delivered | cancelled
    status: Mapped[str] = mapped_column(String(20), default="confirmed")

    customer: Mapped[Customer] = relationship()
    product: Mapped[Product | None] = relationship()
    dispatches: Mapped[list["Dispatch"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class Dispatch(Base):
    __tablename__ = "dispatches"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    dispatched_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    truck_no: Mapped[str] = mapped_column(String(20), default="")
    driver_phone: Mapped[str] = mapped_column(String(20), default="")
    lr_no: Mapped[str] = mapped_column(String(40), default="")
    qty_mt: Mapped[float] = mapped_column(Float, default=0)
    eta: Mapped[date | None] = mapped_column(Date)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Mill test certificate (MTC) for this lot, path under data/mtc/
    mtc_path: Mapped[str] = mapped_column(String(300), default="")
    order: Mapped[Order] = relationship(back_populates="dispatches")


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(40), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    invoice_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[float] = mapped_column(Float)  # including GST
    paid_amount: Mapped[float] = mapped_column(Float, default=0)
    customer: Mapped[Customer] = relationship()

    @property
    def outstanding(self) -> float:
        return round(max(self.amount - self.paid_amount, 0), 2)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    paid_on: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)
    mode: Mapped[str] = mapped_column(String(30), default="")
    ref: Mapped[str] = mapped_column(String(80), default="")


class Prospect(Base):
    """Target account for the FY26-27 import-substitute products (pipeline module)."""

    __tablename__ = "prospects"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    company: Mapped[str] = mapped_column(String(200))
    segment: Mapped[str] = mapped_column(String(60), default="")  # cement | mining | power | earthmoving | steel
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    city: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(String(100), default="")
    contact_name: Mapped[str] = mapped_column(String(120), default="")
    designation: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    current_supplier: Mapped[str] = mapped_column(String(200), default="")
    est_annual_qty_mt: Mapped[float | None] = mapped_column(Float)
    # identified | contacted | sample_sent | trial_running | trial_passed | approved_vendor | first_order | lost
    stage: Mapped[str] = mapped_column(String(30), default="identified")
    next_action: Mapped[str] = mapped_column(String(300), default="")
    next_action_date: Mapped[date | None] = mapped_column(Date)
    rep_id: Mapped[int | None] = mapped_column(ForeignKey("sales_reps.id"))
    notes: Mapped[str] = mapped_column(Text, default="")
    product: Mapped[Product | None] = relationship()
    rep: Mapped[SalesRep | None] = relationship()


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    due_date: Mapped[date | None] = mapped_column(Date)
    rep_id: Mapped[int | None] = mapped_column(ForeignKey("sales_reps.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    prospect_id: Mapped[int | None] = mapped_column(ForeignKey("prospects.id"))
    # reorder_nudge | followup | callback | payment_followup | handoff | pipeline
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(300))
    details: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="open")  # open | done | dismissed
    # Prevents the daily jobs from creating the same task twice.
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    rep: Mapped[SalesRep | None] = relationship()
    customer: Mapped[Customer | None] = relationship()


class Outbox(Base):
    """Every business-initiated message waits here until a person approves it.

    On approval it is delivered to the customer's portal inbox (Notification)
    and, if an address is known and SMTP is configured, by email.
    """

    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    to_name: Mapped[str] = mapped_column(String(200), default="")
    to_email: Mapped[str] = mapped_column(String(200), default="")
    to_phone: Mapped[str] = mapped_column(String(20), default="")  # for staff reference only
    subject: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text)
    attachment_path: Mapped[str] = mapped_column(String(300), default="")
    kind: Mapped[str] = mapped_column(String(30))  # quote | payment_reminder | reorder | brief | outreach | dispatch
    # pending_approval | approved | sent | failed | rejected
    status: Mapped[str] = mapped_column(String(20), default="pending_approval")
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(80), default="")


class PortalUser(Base):
    """A buyer's login to the customer portal.

    Linked to a Customer (staff invite) to see orders, invoices and certificates,
    or only to a Lead (self sign-up) to see their own quotes until they buy.
    """

    __tablename__ = "portal_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    name: Mapped[str] = mapped_column(String(120), default="")
    company: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), unique=True)
    phone: Mapped[str] = mapped_column(String(20), default="")
    password_hash: Mapped[str] = mapped_column(String(200), default="")
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"))
    invite_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    customer: Mapped[Customer | None] = relationship()
    lead: Mapped[Lead | None] = relationship()


class Notification(Base):
    """A message in the buyer's portal inbox."""

    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text)
    quote_id: Mapped[int | None] = mapped_column(ForeignKey("quotes.id"))
    attachment_path: Mapped[str] = mapped_column(String(300), default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class DocumentScan(Base):
    """An uploaded payment proof or invoice, read by OCR and confirmed by staff before it changes any account."""

    __tablename__ = "document_scans"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    kind: Mapped[str] = mapped_column(String(20))  # payment | invoice
    source: Mapped[str] = mapped_column(String(20), default="staff")  # portal | staff
    uploaded_by: Mapped[str] = mapped_column(String(120), default="")
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    file_path: Mapped[str] = mapped_column(String(300))
    original_name: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")  # what the uploader typed
    # processing | needs_review | confirmed | rejected
    status: Mapped[str] = mapped_column(String(20), default="processing")
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    extracted: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str] = mapped_column(String(80), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    customer: Mapped[Customer | None] = relationship()


class Activity(Base):
    """Timeline entries shown on customer, lead and prospect pages."""

    __tablename__ = "activities"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    entity: Mapped[str] = mapped_column(String(20))  # customer | lead | prospect
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    text: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), default="system")


class MarketPrice(Base):
    __tablename__ = "market_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    price_date: Mapped[date] = mapped_column(Date, index=True)
    item: Mapped[str] = mapped_column(String(120))  # e.g. "Sponge iron, Raipur"
    price: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(30), default="INR/MT")
    source: Mapped[str] = mapped_column(String(120), default="")


class Brief(Base):
    __tablename__ = "briefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    brief_date: Mapped[date] = mapped_column(Date, unique=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    narrative: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Counter(Base):
    """Running numbers for quote/order documents, e.g. QT-2026-0001."""

    __tablename__ = "counters"
    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)
