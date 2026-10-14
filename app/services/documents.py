"""Payment proofs and invoices read by Nemotron OCR (+ LLM field extraction), confirmed by staff.

Flow: upload -> process_scan (OCR text -> fields -> checks) -> needs_review -> staff confirm/reject.
Nothing touches an account until a person confirms, because OCR and extraction can be wrong.
"""

import logging
import re
import secrets
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent import llm
from app.agent.prompts import DOC_EXTRACT_SYSTEM
from app.channels.nemotron_ocr import OCRError, ocr_file
from app.config import settings
from app.models import Customer, DocumentScan, Invoice, Order, Outbox, Payment
from app.services import receivables
from app.services.crm import create_task
from app.services.pricing import find_product
from app.services.tally_import import parse_date
from app.util import inr, now, today

log = logging.getLogger(__name__)

ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
MAX_BYTES = 10 * 1024 * 1024
LOW_CONFIDENCE = 0.8

PAYMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {"type": ["number", "null"], "description": "Amount paid/transferred"},
        "paid_on": {"type": ["string", "null"], "description": "Transaction date YYYY-MM-DD"},
        "reference": {"type": ["string", "null"], "description": "UTR / transaction ref / cheque number"},
        "mode": {"type": ["string", "null"], "enum": ["NEFT", "RTGS", "IMPS", "UPI", "Cheque", "Cash", "Other", None]},
        "payer_name": {"type": ["string", "null"]},
        "payer_bank": {"type": ["string", "null"]},
        "payee_name": {"type": ["string", "null"], "description": "Beneficiary / account credited"},
        "invoice_numbers": {"type": "array", "items": {"type": "string"},
                            "description": "Invoice numbers mentioned in remarks"},
    },
}

INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "due_date": {"type": ["string", "null"], "description": "YYYY-MM-DD if printed"},
        "buyer_name": {"type": ["string", "null"], "description": "Billed-to party"},
        "buyer_gstin": {"type": ["string", "null"]},
        "buyer_city": {"type": ["string", "null"]},
        "buyer_state": {"type": ["string", "null"]},
        "item": {"type": ["string", "null"], "description": "Main item description"},
        "qty_mt": {"type": ["number", "null"], "description": "Quantity in metric tonnes"},
        "rate_per_mt": {"type": ["number", "null"]},
        "taxable_value": {"type": ["number", "null"]},
        "gst_amount": {"type": ["number", "null"], "description": "Total of CGST+SGST or IGST"},
        "total_amount": {"type": ["number", "null"], "description": "Invoice total including GST"},
    },
}


# ---------------------------------------------------------------- upload

def save_upload(db: Session, *, kind: str, filename: str, content: bytes, source: str, uploaded_by: str,
                customer_id: int | None = None, note: str = "") -> DocumentScan:
    if kind not in ("payment", "invoice"):
        raise ValueError("Unknown document type")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED:
        raise ValueError("Please upload a PDF or a photo (PNG, JPG, WEBP).")
    if not content:
        raise ValueError("The file is empty.")
    if len(content) > MAX_BYTES:
        raise ValueError("File is larger than 10 MB.")
    folder = settings.data_dir / "scans"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{today():%Y%m%d}-{secrets.token_hex(8)}{suffix}"
    path.write_bytes(content)
    scan = DocumentScan(kind=kind, source=source, uploaded_by=uploaded_by, customer_id=customer_id,
                        file_path=str(path), original_name=Path(filename).name[:200], note=note[:1000])
    db.add(scan)
    db.flush()
    return scan


# ---------------------------------------------------------------- processing

def _num(v) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


def _date(v) -> date | None:
    try:
        return parse_date(str(v)) if v else None
    except ValueError:
        return None


def find_customer(db: Session, name: str | None, gstin: str | None = None) -> Customer | None:
    if gstin:
        cust = db.scalar(select(Customer).where(func.upper(Customer.gstin) == gstin.strip().upper()))
        if cust:
            return cust
    if name:
        clean = re.sub(r"\b(m/s\.?|pvt\.?|private|ltd\.?|limited)\b", "", name.lower()).strip(" .,")
        if len(clean) >= 4:
            for cust in db.scalars(select(Customer)):
                if clean in cust.name.lower() or cust.name.lower() in name.lower():
                    return cust
    return None


def check_payment(db: Session, scan: DocumentScan, f: dict) -> list[str]:
    warns = []
    amount, paid_on, ref = _num(f.get("amount")), _date(f.get("paid_on")), (f.get("reference") or "").strip()
    if not amount or amount <= 0:
        warns.append("Amount not found: enter it from the document.")
    if not ref:
        warns.append("UTR / reference not found.")
    elif db.scalar(select(Payment.id).where(Payment.ref == ref)):
        warns.append(f"UTR {ref} is already recorded: possible duplicate.")
    if paid_on and paid_on > today():
        warns.append("Payment date is in the future.")
    if paid_on and paid_on < today() - timedelta(days=90):
        warns.append("Payment date is more than 90 days old.")
    payee = (f.get("payee_name") or "").lower()
    if payee and not any(k in payee for k in ("hare", "krishna", "shk", "sponge")):
        warns.append(f"Beneficiary on the slip is '{f.get('payee_name')}', not SHK. Check the account credited.")
    if scan.customer_id is None:
        warns.append("Customer not identified: choose the customer before confirming.")
    return warns


def suggest_invoice(db: Session, customer_id: int | None, f: dict) -> int | None:
    if not customer_id:
        return None
    open_invs = receivables.open_invoices(db, customer_id)
    mentioned = {n.strip().upper() for n in (f.get("invoice_numbers") or []) if n}
    for inv in open_invs:
        if inv.number.upper() in mentioned:
            return inv.id
    amount = _num(f.get("amount"))
    for inv in open_invs:
        if amount and abs(inv.outstanding - amount) <= 1:
            return inv.id
    return None


def check_invoice(db: Session, f: dict) -> list[str]:
    """Checks, plus one auto-fix: a quantity read 1000x too big ("30.000" MT read as 30000)."""
    warns = []
    qty, rate, taxable = _num(f.get("qty_mt")), _num(f.get("rate_per_mt")), _num(f.get("taxable_value"))
    if qty and rate and taxable and abs(qty * rate - taxable) > taxable * 0.02:
        if abs(qty / 1000 * rate - taxable) <= taxable * 0.02:
            f["qty_mt"] = round(qty / 1000, 3)
            warns.append(f"Quantity corrected from {qty:g} to {f['qty_mt']:g} MT (qty x rate must equal taxable value).")
        else:
            warns.append(f"Qty {qty:g} MT x rate {inr(rate)} = {inr(qty * rate)}, but taxable value is {inr(taxable)}.")
    number = (f.get("invoice_number") or "").strip()
    if not number:
        warns.append("Invoice number not found.")
    elif db.scalar(select(Invoice.id).where(Invoice.number == number)):
        warns.append(f"Invoice {number} already exists in the system.")
    total, taxable, gst = _num(f.get("total_amount")), _num(f.get("taxable_value")), _num(f.get("gst_amount"))
    if not total:
        warns.append("Invoice total not found.")
    elif taxable and gst and abs(taxable + gst - total) > max(2.0, total * 0.002):
        warns.append(f"Taxable {inr(taxable)} + GST {inr(gst)} doesn't match total {inr(total)} (round-off or misread?).")
    if not _date(f.get("invoice_date")):
        warns.append("Invoice date not found.")
    return warns


def process_scan(db: Session, scan: DocumentScan, *, ocr=ocr_file, client=None) -> DocumentScan:
    """OCR + extraction + checks. Always ends in needs_review so a person decides."""
    try:
        result = ocr(Path(scan.file_path))
        scan.ocr_text, scan.ocr_confidence = result.text, result.confidence
    except OCRError as e:
        scan.error = str(e)
        scan.status = "needs_review"
        scan.warnings = ["OCR could not read this file: enter the details by hand from the document."]
        db.flush()
        return scan

    fields: dict = {}
    if scan.ocr_text.strip():
        schema = PAYMENT_SCHEMA if scan.kind == "payment" else INVOICE_SCHEMA
        what = "a payment proof (bank slip, NEFT/RTGS/UPI screenshot or cheque)" if scan.kind == "payment" \
            else "a sales invoice issued by SHK"
        prompt = f"Document type: {what}.\nUploader's note: {scan.note or '-'}\n\nOCR text:\n<<<\n{scan.ocr_text[:12000]}\n>>>"
        try:
            fields = llm.complete_json(DOC_EXTRACT_SYSTEM, prompt, schema, client=client)
        except llm.LLMUnavailable as e:
            scan.error = f"Field extraction unavailable ({e}); OCR text is shown for manual entry."

    warns: list[str] = []
    if scan.kind == "payment":
        if scan.customer_id is None:
            cust = find_customer(db, fields.get("payer_name"))
            if cust:
                scan.customer_id = cust.id
                warns.append(f"Customer matched by payer name '{fields.get('payer_name')}': please verify.")
        fields["suggested_invoice_id"] = suggest_invoice(db, scan.customer_id, fields)
        warns += check_payment(db, scan, fields)
    else:
        cust = find_customer(db, fields.get("buyer_name"), fields.get("buyer_gstin"))
        if cust:
            scan.customer_id = cust.id
        elif fields.get("buyer_name"):
            warns.append(f"Buyer '{fields['buyer_name']}' isn't a customer yet: a new customer will be created.")
        warns += check_invoice(db, fields)
    if scan.ocr_confidence is not None and scan.ocr_confidence < LOW_CONFIDENCE:
        warns.append(f"Low OCR confidence ({scan.ocr_confidence:.0%}): compare every field with the document.")
    if not scan.ocr_text.strip():
        warns.append("No text found in the document (blank, blurred or upside down?).")

    scan.extracted, scan.warnings, scan.status = fields, warns, "needs_review"
    db.flush()
    return scan


# ---------------------------------------------------------------- staff decisions

def confirm_payment(db: Session, scan: DocumentScan, *, customer_id: int, amount: float, paid_on: date,
                    reference: str, mode: str, invoice_id: int | None, user: str) -> Payment:
    if scan.status != "needs_review":
        raise ValueError(f"This document is already {scan.status}.")
    if amount <= 0:
        raise ValueError("Amount must be more than zero.")
    reference = reference.strip()
    if reference and db.scalar(select(Payment.id).where(Payment.ref == reference)):
        raise ValueError(f"A payment with reference {reference} is already recorded.")
    cust = db.get(Customer, customer_id)
    if cust is None:
        raise ValueError("Choose the customer.")
    if invoice_id:
        inv = db.get(Invoice, invoice_id)
        if inv is None or inv.customer_id != cust.id:
            raise ValueError("That invoice doesn't belong to this customer.")
    pay = receivables.record_payment(db, cust.id, amount, paid_on, mode=mode, ref=reference, invoice_id=invoice_id)
    scan.payment_id, scan.customer_id = pay.id, cust.id
    scan.status, scan.reviewed_by, scan.reviewed_at = "confirmed", user, now()
    if scan.source == "portal":
        db.add(Outbox(customer_id=cust.id, to_name=cust.name, kind="payment_received", status="approved",
                      decided_by=user, subject="Payment received - Shri Hare-Krishna Sponge Iron",
                      body=(f"Thank you. We have received your payment of {inr(amount)} dated {paid_on:%d-%m-%Y}"
                            f"{' (ref ' + reference + ')' if reference else ''} and updated your account.")))
    db.flush()
    return pay


def confirm_invoice(db: Session, scan: DocumentScan, *, customer_id: int | None, new_customer_name: str,
                    gstin: str, city: str, state: str, number: str, invoice_date: date, due_date: date | None,
                    amount: float, item: str, qty_mt: float | None, rate_per_mt: float | None, user: str) -> Invoice:
    if scan.status != "needs_review":
        raise ValueError(f"This document is already {scan.status}.")
    number = number.strip()
    if not number or amount <= 0:
        raise ValueError("Invoice number and total are required.")
    if db.scalar(select(Invoice.id).where(Invoice.number == number)):
        raise ValueError(f"Invoice {number} already exists.")
    cust = db.get(Customer, customer_id) if customer_id else None
    if cust is None:
        if not new_customer_name.strip():
            raise ValueError("Choose a customer or enter the new customer's name.")
        cust = Customer(name=new_customer_name.strip(), gstin=gstin.strip().upper(), city=city, state=state,
                        source="invoice_scan")
        db.add(cust)
        db.flush()
    order_id = None
    if qty_mt:
        product = find_product(db, item)
        order = Order(number=f"I-{number}", customer_id=cust.id, order_date=invoice_date,
                      product_id=product.id if product else None, item_text=item, qty_mt=qty_mt,
                      rate_per_mt=rate_per_mt or 0, amount=round((rate_per_mt or 0) * qty_mt, 2),
                      destination=cust.city, status="delivered")
        db.add(order)
        db.flush()
        order_id = order.id
    inv = Invoice(number=number, customer_id=cust.id, order_id=order_id, invoice_date=invoice_date,
                  due_date=due_date or invoice_date + timedelta(days=cust.credit_days), amount=amount)
    db.add(inv)
    db.flush()
    scan.invoice_id, scan.customer_id = inv.id, cust.id
    scan.status, scan.reviewed_by, scan.reviewed_at = "confirmed", user, now()
    db.flush()
    return inv


def reject(db: Session, scan: DocumentScan, user: str, reason: str = "") -> None:
    if scan.status == "confirmed":
        raise ValueError("Already confirmed.")
    scan.status, scan.reviewed_by, scan.reviewed_at = "rejected", user, now()
    scan.error = (scan.error + f"\nRejected: {reason}").strip()
    if scan.source == "portal" and scan.customer_id:
        db.add(Outbox(customer_id=scan.customer_id, to_name="", kind="payment_received", status="approved",
                      decided_by=user, subject="About your payment proof - Shri Hare-Krishna Sponge Iron",
                      body=("We couldn't verify the payment proof you uploaded"
                            f"{' (' + reason + ')' if reason else ''}. Please upload a clearer copy or contact "
                            "our accounts team.")))
    db.flush()


def notify_staff_of_upload(db: Session, scan: DocumentScan) -> None:
    who = scan.customer.name if scan.customer else scan.uploaded_by
    create_task(db, kind="followup", title=f"Verify payment proof from {who}",
                details="Open Staff → Documents to check and confirm it.", customer_id=scan.customer_id,
                rep_id=scan.customer.rep_id if scan.customer else None, due_date=today(),
                dedupe_key=f"scan:{scan.id}")


def process_in_background(scan_id: int) -> None:
    """Run OCR after the upload request has returned (OCR + extraction can take several seconds)."""
    from app.db import session_scope

    try:
        with session_scope() as db:
            scan = db.get(DocumentScan, scan_id)
            if scan is None:
                return
            process_scan(db, scan)
            if scan.source == "portal":
                notify_staff_of_upload(db, scan)
    except Exception:
        log.exception("processing document %s failed", scan_id)
        with session_scope() as db:
            scan = db.get(DocumentScan, scan_id)
            if scan and scan.status == "processing":
                scan.status, scan.error = "needs_review", "Processing failed; enter the details by hand."
