import pytest
from sqlalchemy import select

from app.models import Outbox, Task
from app.services import crm, pipeline, receivables, reorder, tally_import
from app.services.pricing import PricingError, price_quote, save_quote
from app.services.quote_pdf import render_quote_pdf
from app.util import inr, norm_phone, today
from tests.conftest import add_invoice, add_orders


# ---- pricing

def test_delivered_rate_adds_premium_terms_and_freight(db):
    calc = price_quote(db, [{"product": "billet", "variant": "125x125 C18", "qty_mt": 30}], "Nagpur", "credit_15")
    line = calc.lines[0]
    # 40500 base + 300 premium + 350 credit adder + 1100 freight
    assert line.rate_per_mt == 42250
    assert calc.subtotal == 1267500
    assert calc.gst_total == pytest.approx(228150)
    assert "credit terms for a new party" in calc.approval_reasons


def test_small_advance_quote_is_auto_approved(db):
    calc = price_quote(db, [{"product": "sponge iron", "qty_mt": 20}], "ex-works", "advance")
    assert calc.lines[0].freight_per_mt == 0
    assert not calc.needs_approval
    quote = save_quote(db, calc, party_name="Test")
    assert quote.status == "approved" and quote.number.startswith("QT-")
    assert render_quote_pdf(db, quote).exists()


def test_large_or_discounted_quotes_need_approval(db, customer):
    big = price_quote(db, [{"product": "DRI", "qty_mt": 500}], "Raipur", customer=customer)
    assert any("auto-approval limit" in r for r in big.approval_reasons)
    disc = price_quote(db, [{"product": "DRI", "qty_mt": 10}], "Raipur", discount_per_mt=200, customer=customer)
    assert any("discount" in r for r in disc.approval_reasons)
    within_credit = price_quote(db, [{"product": "DRI", "qty_mt": 10}], "Raipur", "credit_15", customer=customer)
    assert not within_credit.needs_approval


@pytest.mark.parametrize("items,dest,msg", [
    ([{"product": "DRI", "qty_mt": 20}], "Kolkata", "No freight rate"),
    ([{"product": "grinding media balls", "qty_mt": 20}], "Raipur", "not in regular production"),
    ([{"product": "billet", "qty_mt": 5}], "Raipur", "Minimum order"),
    ([{"product": "DRI", "variant": "pellets", "qty_mt": 20}], "Raipur", "Unknown size/grade"),
    ([{"product": "copper wire", "qty_mt": 20}], "Raipur", "not in the catalogue"),
])
def test_pricing_refuses_to_guess(db, items, dest, msg):
    with pytest.raises(PricingError, match=msg):
        price_quote(db, items, dest)


def test_missing_daily_rate_blocks_quote(db):
    from app.models import DailyRate

    for r in db.scalars(select(DailyRate)):
        db.delete(r)
    db.flush()
    with pytest.raises(PricingError, match="rate .* has not been entered"):
        price_quote(db, [{"product": "DRI", "qty_mt": 20}], "Raipur")


# ---- CRM

def test_upsert_lead_scores_routes_and_merges(db, rep):
    lead = crm.upsert_lead(db, "98260 12345", "web", {"product": "sponge iron", "city": "Durg", "state": "Chhattisgarh"})
    assert lead.phone == "919826012345" and lead.status == "new" and lead.rep_id == rep.id
    again = crm.upsert_lead(db, "+91-9826012345", "web", {"qty_mt": "120", "frequency": "monthly", "company": "Ravi Steels"})
    assert again.id == lead.id
    assert again.status == "qualified"
    assert again.score == 15 + 15 + 15 + 10 + 15 + 5


def test_lead_from_existing_customer_links_and_uses_customer_rep(db, customer):
    lead = crm.upsert_lead(db, customer.phone, "web", {"product": "billet", "state": "Maharashtra"})
    assert lead.customer_id == customer.id and lead.rep_id == customer.rep_id


def test_norm_phone_and_inr():
    assert norm_phone("09826420222") == "919826420222"
    assert norm_phone("+91 98264 20222") == "919826420222"
    assert inr(1234567.5) == "₹12,34,567.50"
    assert inr(999) == "₹999.00"


# ---- reorder (module 3)

def test_lapsed_customer_gets_one_task_and_one_draft(db, customer):
    add_orders(db, customer, [70, 60, 50, 40, 30])  # every 10 days, quiet for 30
    cyc = next(c for c in reorder.all_cycles(db) if c.customer.id == customer.id)
    assert cyc.usual_gap_days == 10 and cyc.days_since == 30 and cyc.lapsed
    assert reorder.run_reorder_scan(db) == {"tasks_created": 1, "messages_drafted": 1}
    assert reorder.run_reorder_scan(db) == {"tasks_created": 0, "messages_drafted": 0}
    assert db.scalar(select(Outbox)).status == "pending_approval"


def test_regular_buyer_is_not_flagged(db, customer):
    add_orders(db, customer, [40, 30, 20, 10, 2])
    assert reorder.run_reorder_scan(db)["tasks_created"] == 0


# ---- receivables (module 4)

def test_ageing_buckets_and_reminder_schedule(db, customer):
    add_invoice(db, customer, "INV-1", 7, 100000)       # 7 days overdue -> reminder
    add_invoice(db, customer, "INV-2", 45, 50000, 20000)  # 31-60 bucket, no reminder today
    add_invoice(db, customer, "INV-3", 30, 80000)       # escalate -> task + reminder
    row = receivables.ageing(db)[0]
    assert row.buckets["0-30"] == 180000 and row.buckets["31-60"] == 30000
    assert receivables.run_payment_reminders(db) == {"reminders_drafted": 2, "escalated": 1}
    assert receivables.run_payment_reminders(db) == {"reminders_drafted": 0, "escalated": 0}
    assert db.scalar(select(Task)).kind == "payment_followup"


def test_payment_applies_to_oldest_invoice_first(db, customer):
    old = add_invoice(db, customer, "INV-A", 40, 1000)
    new = add_invoice(db, customer, "INV-B", 5, 1000)
    receivables.record_payment(db, customer.id, 1500, today())
    assert old.outstanding == 0 and new.outstanding == 500


# ---- pipeline (module 5)

def test_prospect_gets_product_from_segment_and_followups(db):
    p = pipeline.add_prospect(db, {"company": "Acme Cement", "segment": "cement"})
    assert p.product.code == "GMB" and p.stage == "identified"
    p.next_action_date = today()
    assert pipeline.run_pipeline_followups(db) == {"pipeline_tasks": 1}
    pipeline.set_stage(db, p, "sample_sent")
    assert p.next_action.startswith("Confirm sample")
    assert pipeline.funnel(db)["sample_sent"] == 1


def test_prospect_csv_import_skips_duplicates(db):
    csv_text = "company,segment,city\nAcme Cement,cement,Raipur\nDeep Mines,mining,Korba\nacme cement,cement,Raipur\n"
    assert pipeline.import_prospects_csv(db, csv_text) == 2


# ---- Tally import

def test_tally_sales_and_receipts_import(db):
    sales = (
        "Date,Voucher_No,Party,Phone,City,State,Item,Qty_MT,Rate,Amount\n"
        "01-09-2026,S/101,Bhilai Steels,9826000001,Bhilai,Chhattisgarh,Sponge Iron,50,27000,1593000\n"
        "11-09-2026,S/102,Bhilai Steels,9826000001,Bhilai,Chhattisgarh,Sponge Iron,40,27200,1283840\n"
        "bad-date,S/103,Bhilai Steels,,,,DRI,1,1,1\n"
    )
    res = tally_import.import_sales(db, sales)
    assert res["created"] == 2 and len(res["errors"]) == 1
    assert tally_import.import_sales(db, sales)["skipped_existing"] == 2
    res = tally_import.import_receipts(db, "date,party,amount,ref\n2026-09-20,Bhilai Steels,1593000,UTR1\n")
    assert res["created"] == 1
    invs = receivables.open_invoices(db)
    assert [i.number for i in invs] == ["S/102"]
