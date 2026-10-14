"""Import sales history and receipts exported from Tally (or any ERP) as CSV.

Sales register columns (header names, case-insensitive):
    date, voucher_no, party, gstin, phone, city, state, item, qty_mt, rate, amount, due_date
  - amount is the invoice value including GST; due_date is optional (defaults to date + party credit days)
Receipts columns:
    date, party, amount, mode, ref, invoice_no (optional)

Dates accept 2026-04-01, 01-04-2026, 01/04/2026 or 1-Apr-2026. Re-importing the
same voucher numbers is safe: existing invoices are skipped.
"""

import csv
import io
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Customer, Invoice, Order
from app.services.pricing import find_product
from app.services.receivables import record_payment
from app.util import norm_phone

DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%b-%y", "%d.%m.%Y")


def parse_date(s: str) -> date:
    s = (s or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date '{s}'")


def _num(s) -> float:
    s = str(s or "").replace(",", "").replace("₹", "").strip()
    return float(s) if s else 0.0


def _rows(text: str):
    for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        yield {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}


def _customer(db: Session, row: dict) -> Customer:
    name = row["party"]
    cust = db.scalar(select(Customer).where(func.lower(Customer.name) == name.lower()))
    if cust is None:
        cust = Customer(name=name, source="tally")
        db.add(cust)
    for field in ("gstin", "city", "state"):
        if row.get(field) and not getattr(cust, field):
            setattr(cust, field, row[field])
    if row.get("phone") and not cust.phone:
        cust.phone = norm_phone(row["phone"])
    db.flush()
    return cust


def import_sales(db: Session, text: str) -> dict:
    created = skipped = 0
    errors: list[str] = []
    for i, row in enumerate(_rows(text), start=2):
        try:
            if not row.get("party") or not row.get("voucher_no"):
                raise ValueError("party and voucher_no are required")
            if db.scalar(select(Invoice.id).where(Invoice.number == row["voucher_no"])):
                skipped += 1
                continue
            cust = _customer(db, row)
            d = parse_date(row["date"])
            qty, amount = _num(row.get("qty_mt")), _num(row.get("amount"))
            product = find_product(db, row.get("item", ""))
            order = Order(
                number=f"T-{row['voucher_no']}", customer_id=cust.id, order_date=d,
                product_id=product.id if product else None, item_text=row.get("item", ""),
                qty_mt=qty, rate_per_mt=_num(row.get("rate")), amount=amount,
                destination=row.get("city", ""), status="delivered",
            )
            db.add(order)
            db.flush()
            due = parse_date(row["due_date"]) if row.get("due_date") else d + timedelta(days=cust.credit_days)
            db.add(Invoice(number=row["voucher_no"], customer_id=cust.id, order_id=order.id,
                           invoice_date=d, due_date=due, amount=amount))
            db.flush()
            created += 1
        except (ValueError, KeyError) as e:
            errors.append(f"row {i}: {e}")
    return {"created": created, "skipped_existing": skipped, "errors": errors}


def import_receipts(db: Session, text: str) -> dict:
    created = 0
    errors: list[str] = []
    for i, row in enumerate(_rows(text), start=2):
        try:
            cust = db.scalar(select(Customer).where(func.lower(Customer.name) == row["party"].lower()))
            if cust is None:
                raise ValueError(f"unknown party '{row['party']}'")
            inv_id = None
            if row.get("invoice_no"):
                inv = db.scalar(select(Invoice).where(Invoice.number == row["invoice_no"]))
                inv_id = inv.id if inv else None
            record_payment(db, cust.id, _num(row["amount"]), parse_date(row["date"]),
                           mode=row.get("mode", ""), ref=row.get("ref", ""), invoice_id=inv_id)
            created += 1
        except (ValueError, KeyError) as e:
            errors.append(f"row {i}: {e}")
    return {"created": created, "errors": errors}
