"""Tools available to the customer-facing sales agent.

Security rule: anything account-specific (orders, statements, certificates) is
looked up from the logged-in portal user's customer account, never from what
the model or the buyer typed. Anonymous website visitors have no account access,
and a buyer can only ever see their own account.
"""

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Conversation, Customer, Order, Outbox, Product, Quote
from app.services import approvals, crm, pipeline
from app.services.orders import order_summary, recent_orders
from app.services.pricing import PricingError, find_product, price_quote, save_quote
from app.services.quote_pdf import render_quote_pdf
from app.services.receivables import statement_text
from app.util import inr, norm_phone, now


@dataclass
class ToolContext:
    db: Session
    conversation: Conversation
    phone: str  # contact phone if known (portal login); empty for anonymous web chats
    customer: Customer | None
    attachments: list[Path] = field(default_factory=list)


def _str_props(*names: str) -> dict:
    return {n: {"type": "string"} for n in names}


TOOLS: list[dict] = [
    {
        "name": "get_product_catalog",
        "description": (
            "Look up SHK's products: specifications, available sizes/grades, minimum order and whether the product "
            "is available now or upcoming. Call with no product to list everything. Contains no prices."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"product": {"type": "string", "description": "Product name or keyword, e.g. 'billet', 'steel shots'"}},
        },
    },
    {
        "name": "save_enquiry_details",
        "description": (
            "Record what the buyer has told you about their requirement. Call whenever you learn something new; "
            "only include fields you actually know. Returns which key details are still missing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_str_props("name", "company", "email", "phone", "city", "state", "product", "variant_text",
                             "timeline", "requirement"),
                "qty_mt": {"type": "number", "description": "Quantity in metric tonnes"},
                "frequency": {"type": "string", "description": "one-time, weekly, monthly..."},
                "payment_terms": {"type": "string", "description": "advance, credit 15 days..."},
            },
        },
    },
    {
        "name": "get_price_quote",
        "description": (
            "Price a requirement at today's rate and create a quotation. Needs product, quantity and delivery "
            "city (or 'ex-works'). If approved, a PDF download link is attached to your reply automatically; share the rate "
            "per MT and total. If it needs approval or can't be priced, the result says so: then do not share any "
            "number and tell the buyer the team will confirm shortly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product": {"type": "string"},
                            "variant": {"type": "string", "description": "Size or grade, if the buyer gave one"},
                            "qty_mt": {"type": "number"},
                        },
                        "required": ["product", "qty_mt"],
                    },
                },
                "destination": {"type": "string", "description": "Delivery city, or 'ex-works'"},
                "payment_terms": {
                    "type": "string",
                    "enum": ["advance", "against_delivery", "credit_15", "credit_30"],
                },
            },
            "required": ["items", "destination"],
        },
    },
    {
        "name": "accept_quote",
        "description": (
            "The buyer has clearly confirmed they want to order against a quote. Notifies the sales team to "
            "confirm the order, PO and payment. Does not itself book production."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"quote_number": {"type": "string"}, "buyer_note": {"type": "string"}},
            "required": ["quote_number"],
        },
    },
    {
        "name": "get_my_orders",
        "description": "Order and dispatch status for the logged-in customer (truck no, LR, ETA).",
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": "string", "description": "Optional; latest orders if blank"}},
        },
    },
    {
        "name": "get_my_account_statement",
        "description": "Outstanding invoices and total due for the logged-in customer.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_test_certificate",
        "description": "Give the logged-in customer a download link for the mill test certificate (MTC) of a dispatched order.",
        "input_schema": {
            "type": "object",
            "properties": {"order_number": {"type": "string"}},
            "required": ["order_number"],
        },
    },
    {
        "name": "register_new_product_interest",
        "description": (
            "Buyer is interested in an upcoming product (grinding media balls, tooth points, alloy castings). "
            "Adds them to the launch pipeline so the team contacts them for trials."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **_str_props("company", "product", "segment", "city", "state", "contact_name", "designation",
                             "current_supplier", "notes"),
                "est_annual_qty_mt": {"type": "number"},
            },
            "required": ["company", "product"],
        },
    },
    {
        "name": "request_human",
        "description": (
            "Hand the conversation to a salesperson: complaints, quality claims, negotiation, credit, legal, or "
            "when the buyer asks for a person. Set takeover=true to stop replying yourself until staff release it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "urgency": {"type": "string", "enum": ["normal", "high"]},
                "takeover": {"type": "boolean"},
                "callback_time": {"type": "string", "description": "When the buyer wants a call, if they said"},
            },
            "required": ["reason"],
        },
    },
]


def _missing_fields(lead) -> list[str]:
    missing = []
    if not lead.product_id:
        missing.append("product")
    if not lead.qty_mt:
        missing.append("quantity (MT)")
    if not (lead.city or lead.state):
        missing.append("delivery location")
    if not lead.company:
        missing.append("company name")
    if not lead.frequency:
        missing.append("one-time or regular requirement")
    return missing


def get_product_catalog(ctx: ToolContext, product: str = "") -> dict:
    db = ctx.db
    if product:
        p = find_product(db, product)
        items = [p] if p else []
        if not items:
            return {"error": f"No product matching '{product}'.",
                    "available": [x.name for x in db.scalars(select(Product))]}
    else:
        items = list(db.scalars(select(Product).order_by(Product.id)))
    return {"products": [
        {
            "name": p.name,
            "status": "available" if p.status == "live" else "upcoming (FY 2026-27), not orderable yet",
            "sizes_grades": [v.name for v in p.variants],
            "min_order_mt": p.min_order_mt or None,
            "gst": f"{p.gst_rate * 100:g}%",
            "spec": p.spec,
        }
        for p in items
    ]}


def save_enquiry_details(ctx: ToolContext, **fields) -> dict:
    phone = fields.pop("phone", "") or ""
    lead = crm.upsert_lead(ctx.db, ctx.phone, ctx.conversation.channel, fields, lead=ctx.conversation.lead)
    if phone and not lead.phone:
        # Contact number typed in chat: stored for call-back, never used to identify an account.
        lead.phone = norm_phone(phone)
    ctx.conversation.lead_id = lead.id
    ctx.conversation.lead = lead
    return {"saved": True, "lead_status": lead.status, "still_missing": _missing_fields(lead)}


def get_price_quote(ctx: ToolContext, items: list, destination: str, payment_terms: str = "advance") -> dict:
    db = ctx.db
    if ctx.conversation.lead is None:
        # Every quote hangs off a lead so it shows in the CRM and the web download check works.
        first = items[0] if items else {}
        save_enquiry_details(ctx, product=first.get("product"), qty_mt=first.get("qty_mt"),
                             variant_text=first.get("variant"), city=destination)
    lead = ctx.conversation.lead
    try:
        calc = price_quote(db, items, destination, payment_terms or "advance", customer=ctx.customer)
    except PricingError as e:
        owner = lead.rep_id if lead else (ctx.customer.rep_id if ctx.customer else None)
        crm.create_task(
            db, kind="followup", title=f"Manual quote needed for {_who(ctx)}",
            details=f"{e}\nRequest: {items} to {destination}, terms {payment_terms}",
            lead_id=lead.id if lead else None, customer_id=ctx.customer.id if ctx.customer else None, rep_id=owner,
        )
        return {"status": "cannot_price_automatically", "reason": str(e),
                "instruction": "Do not share any price. Tell the buyer the sales team will send the rate shortly."}

    party = ctx.customer.name if ctx.customer else (lead.company or lead.name if lead else "") or "Valued Buyer"
    quote = save_quote(db, calc, party_name=party, party_phone=ctx.phone,
                       lead_id=lead.id if lead else None, customer_id=ctx.customer.id if ctx.customer else None)
    if lead and lead.status in ("new", "qualified"):
        lead.status = "quoted"
    if quote.status == "pending_approval":
        db.add(Outbox(
            to_phone=ctx.phone, to_name=party, kind="quote", quote_id=quote.id,
            customer_id=ctx.customer.id if ctx.customer else None, lead_id=lead.id if lead else None,
            body=(f"Your quotation {quote.number} is ready: total {inr(quote.grand_total)} incl. GST, valid today. "
                  f"The PDF is attached and in your portal under Quotes."),
        ))
        crm.create_task(
            db, kind="followup", title=f"Approve quote {quote.number} ({inr(quote.subtotal)}) for {party}",
            details=quote.approval_reason, lead_id=lead.id if lead else None,
            customer_id=ctx.customer.id if ctx.customer else None,
            rep_id=lead.rep_id if lead else (ctx.customer.rep_id if ctx.customer else None),
        )
        return {"status": "pending_approval", "quote_number": quote.number,
                "instruction": "Do not share any price. Tell the buyer the rate is being confirmed by the sales "
                               "team and the quotation will appear in their portal inbox (and email) shortly."}

    ctx.attachments.append(render_quote_pdf(db, quote))
    quote.status = "sent"
    return {
        "status": "approved",
        "quote_number": quote.number,
        "valid_for": "today only",
        "payment_terms": calc.payment_term.label,
        "delivery": destination,
        "lines": [
            {"product": f"{line.product.name} {line.variant}".strip(), "qty_mt": line.qty_mt,
             "rate_per_mt_before_gst": inr(line.rate_per_mt), "gst": f"{line.product.gst_rate * 100:g}%"}
            for line in calc.lines
        ],
        "subtotal": inr(calc.subtotal),
        "gst": inr(calc.gst_total),
        "total_incl_gst": inr(calc.grand_total),
        "pdf": "download link attached to your reply",
    }


def accept_quote(ctx: ToolContext, quote_number: str, buyer_note: str = "") -> dict:
    quote = ctx.db.scalar(select(Quote).where(Quote.number == quote_number.strip().upper()))
    conv = ctx.conversation
    owns = quote is not None and (
        (conv.lead_id is not None and quote.lead_id == conv.lead_id)
        or (ctx.customer is not None and quote.customer_id == ctx.customer.id)
    )
    if not owns:
        return {"error": "No such quote in this conversation or account."}
    try:
        approvals.buyer_accepts(ctx.db, quote, buyer_note, via="chat")
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, "next": "Sales team will call to confirm the order, PO and payment details."}


NOT_LOGGED_IN = {"error": "Account details are only available to customers logged in to the SHK customer portal. "
                          "Ask them to log in at /portal (or contact the sales team for a login)."}


def _who(ctx: ToolContext) -> str:
    lead = ctx.conversation.lead
    if ctx.customer:
        return ctx.customer.name
    return (lead.company or lead.name if lead else "") or ctx.phone or "website visitor"


def get_my_orders(ctx: ToolContext, order_number: str = "") -> dict:
    cust = ctx.customer
    if cust is None:
        return NOT_LOGGED_IN
    if order_number:
        o = ctx.db.scalar(select(Order).where(Order.number == order_number.strip().upper(), Order.customer_id == cust.id))
        if o is None:
            return {"error": f"No order {order_number} on this account."}
        return {"orders": [order_summary(o)]}
    return {"orders": [order_summary(o) for o in recent_orders(ctx.db, cust.id)]}


def get_my_account_statement(ctx: ToolContext) -> dict:
    cust = ctx.customer
    if cust is None:
        return NOT_LOGGED_IN
    return {"customer": cust.name, "statement": statement_text(ctx.db, cust)}


def send_test_certificate(ctx: ToolContext, order_number: str) -> dict:
    cust = ctx.customer
    if cust is None:
        return NOT_LOGGED_IN
    o = ctx.db.scalar(select(Order).where(Order.number == order_number.strip().upper(), Order.customer_id == cust.id))
    if o is None:
        return {"error": f"No order {order_number} on this account."}
    paths = [Path(d.mtc_path) for d in o.dispatches if d.mtc_path and Path(d.mtc_path).exists()]
    if not paths:
        crm.create_task(ctx.db, kind="followup", title=f"Send MTC for {o.number} to {cust.name}",
                        customer_id=cust.id, rep_id=cust.rep_id)
        return {"status": "not_uploaded_yet", "next": "QC team has been asked to share it."}
    ctx.attachments.extend(paths)
    return {"status": "attached", "files": len(paths)}


def register_new_product_interest(ctx: ToolContext, **data) -> dict:
    data["phone"] = ctx.phone
    p = pipeline.add_prospect(ctx.db, data, actor="chat")
    pipeline.set_stage(ctx.db, p, "contacted", actor="chat", note="Inbound interest via chat")
    return {"ok": True, "next": "Product team will contact them about trial lots and launch timing."}


def request_human(ctx: ToolContext, reason: str, urgency: str = "normal", takeover: bool = False,
                  callback_time: str = "") -> dict:
    conv = ctx.conversation
    lead = conv.lead
    rep_id = (ctx.customer.rep_id if ctx.customer else None) or (lead.rep_id if lead else None)
    who = _who(ctx)
    crm.create_task(
        ctx.db, kind="handoff",
        title=f"{'URGENT: ' if urgency == 'high' else ''}{who} needs a person: {reason}",
        details=f"Callback: {callback_time or 'asap'}. Conversation #{conv.id}.",
        customer_id=ctx.customer.id if ctx.customer else None, lead_id=lead.id if lead else None, rep_id=rep_id,
        due_date=now().date(),
    )
    if takeover:
        conv.human_takeover = True
    return {"ok": True, "message_for_buyer": "A member of the sales team will contact you shortly."}


HANDLERS = {
    "get_product_catalog": get_product_catalog,
    "save_enquiry_details": save_enquiry_details,
    "get_price_quote": get_price_quote,
    "accept_quote": accept_quote,
    "get_my_orders": get_my_orders,
    "get_my_account_statement": get_my_account_statement,
    "send_test_certificate": send_test_certificate,
    "register_new_product_interest": register_new_product_interest,
    "request_human": request_human,
}
