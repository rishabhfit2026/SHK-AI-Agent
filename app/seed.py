"""Seed the catalogue and settings tables; optionally load demo data.

    uv run python -m app.seed          # catalogue, payment terms, freight table (safe to re-run)
    uv run python -m app.seed --demo   # + sample reps, customers, history, prospects, rates

Product descriptions come from shkraipur.com. Sizes, premiums, freight and all
demo figures are PLACEHOLDERS: replace them with SHK's real numbers before go-live.
"""

import random
import sys
from datetime import timedelta

from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import (
    Contact, Customer, DailyRate, Dispatch, FreightRate, Invoice, Lead, MarketPrice, Order, PaymentTerm,
    Product, ProductVariant, SalesRep,
)
from app.services import pipeline
from app.util import now, today

PRODUCTS = [
    dict(code="DRI", name="Sponge Iron (DRI)", category="sponge_iron", hsn="7203", min_order_mt=10,
         spec="Coal-based DRI from 100 TPD rotary kiln at 950-1050°C, ~10 h retention, cooled below 120°C. "
              "High metallic iron and degree of metallisation, uniform chemistry, low sulphur and phosphorus. "
              "Lets secondary steel makers make finer grades than with scrap alone.",
         variants=[("Lumps 3-20 mm", 0), ("Fines 0-3 mm", -3000), ("Mixed", -1000)]),
    dict(code="BILLET", name="MS Billets", category="billet", hsn="7207", min_order_mt=25,
         spec="Continuously cast MS billets to IS 2830:2012 (grades C15/C18/C20, MMn/HMn variants; S & P max "
              "0.040-0.050%, CE max 0.39-0.42). For rolling TMT Fe 415/500/550/600. Magnetic particle test for "
              "surface cracks, sulphur print test for internal defects. Length 3-9 m.",
         variants=[("100x100 C18", 0), ("125x125 C18", 300), ("100x100 C20 MMn", 500), ("125x125 C20 HMn", 900)]),
    dict(code="SECTION", name="Structural Sections", category="section", hsn="7216", min_order_mt=5,
         spec="Re-rolled MS sections: angles, channels, beams (rolling mill since 2007).",
         variants=[("Angle 50x50", 0), ("Angle 65x65", 200), ("Channel 75x40", 400), ("Channel 100x50", 500),
                   ("Beam (Joist) 100x50", 800)]),
    dict(code="SHOTS", name="Steel Shots & Grits", category="shots_grits", hsn="7205", min_order_mt=1,
         spec="Cast steel shots (0.425-2.5 mm) and grits from induction-melted steel, centrifugally atomised, "
              "double quenched, tempered and screened. For shot blasting, peening, surface preparation.",
         variants=[("Shot S-170", 0), ("Shot S-230", 0), ("Shot S-330", 500), ("Shot S-390", 500),
                   ("Shot S-460", 800), ("Grit G-25", 1500), ("Grit G-40", 1500), ("Grit G-50", 1500)]),
    dict(code="GMB", name="Grinding Media Balls", category="new_product", hsn="7325", status="upcoming",
         spec="Upcoming FY 2026-27 (import substitute). Cast/forged balls for cement and mineral grinding mills."),
    dict(code="TOOTH", name="Tooth Points", category="new_product", hsn="7325", status="upcoming",
         spec="Upcoming FY 2026-27 (import substitute). Bucket tooth points for excavators and loaders in mining."),
    dict(code="CAST", name="Alloy Steel Castings", category="new_product", hsn="7325", status="upcoming",
         spec="Upcoming FY 2026-27. Alloy steel castings to drawing from the induction furnace and casting shop."),
]

PAYMENT_TERMS = [
    ("advance", "100% advance", 0, 0),
    ("against_delivery", "Payment against delivery", 0, 150),
    ("credit_15", "15 days credit", 15, 350),
    ("credit_30", "30 days credit", 30, 700),
]

# city, state, freight INR/MT from Siltara (placeholder)
FREIGHT = [
    ("Raipur", "Chhattisgarh", 200), ("Siltara", "Chhattisgarh", 0), ("Bhilai", "Chhattisgarh", 350),
    ("Durg", "Chhattisgarh", 380), ("Bilaspur", "Chhattisgarh", 450), ("Raigarh", "Chhattisgarh", 650),
    ("Korba", "Chhattisgarh", 600), ("Nagpur", "Maharashtra", 1100), ("Wardha", "Maharashtra", 1250),
    ("Jalna", "Maharashtra", 1900), ("Aurangabad", "Maharashtra", 2000), ("Mumbai", "Maharashtra", 2700),
    ("Pune", "Maharashtra", 2600), ("Jabalpur", "Madhya Pradesh", 1300), ("Indore", "Madhya Pradesh", 2100),
    ("Bhopal", "Madhya Pradesh", 1800), ("Satna", "Madhya Pradesh", 1500), ("Sambalpur", "Odisha", 900),
    ("Rourkela", "Odisha", 1200), ("Hyderabad", "Telangana", 2200),
]


def seed_catalogue(db) -> None:
    for spec in PRODUCTS:
        spec = dict(spec)
        variants = spec.pop("variants", [])
        p = db.scalar(select(Product).where(Product.code == spec["code"]))
        if p is None:
            p = Product(**spec)
            db.add(p)
            for name, prem in variants:
                p.variants.append(ProductVariant(name=name, premium_per_mt=prem))
    for code, label, days, adder in PAYMENT_TERMS:
        if db.get(PaymentTerm, code) is None:
            db.add(PaymentTerm(code=code, label=label, days=days, adder_per_mt=adder))
    for city, state, rate in FREIGHT:
        if db.scalar(select(FreightRate).where(FreightRate.city == city.lower())) is None:
            db.add(FreightRate(city=city.lower(), state=state, per_mt=rate))
    db.flush()


DEMO_LOGIN = ("buyer@urla-demo.in", "demo-buyer-123")

BASE_RATES = {"DRI": 27500, "BILLET": 40500, "SECTION": 46000, "SHOTS": 68000}


def seed_demo(db) -> None:
    if db.scalar(select(SalesRep.id)):
        print("demo data already present, skipping")
        return
    rnd = random.Random(7)
    reps = [
        SalesRep(name="Amit Agrawal", phone="919800000001", states="Chhattisgarh,Odisha", categories=""),
        SalesRep(name="Sneha Verma", phone="919800000002", states="Maharashtra,Telangana", categories=""),
        SalesRep(name="Rakesh Sahu", phone="919800000003", states="Madhya Pradesh", categories=""),
        SalesRep(name="Priya Nair", phone="919800000004", states="", categories="shots_grits,new_product"),
    ]
    db.add_all(reps)
    db.flush()
    products = {p.code: p for p in db.scalars(select(Product))}

    customers = [
        # name, city, state, segment, credit_days, product, cycle_days, qty, phone, rep
        ("Bhilai Induction Steels Pvt Ltd", "Bhilai", "Chhattisgarh", "induction furnace", 15, "DRI", 7, 150, "919900000101", 0),
        ("Urla Ispat Pvt Ltd", "Raipur", "Chhattisgarh", "induction furnace", 7, "DRI", 5, 100, "919900000102", 0),
        ("Vidarbha TMT Rolling Mills", "Nagpur", "Maharashtra", "rolling mill", 15, "BILLET", 10, 250, "919900000103", 1),
        ("Jalna Steel Re-Rollers", "Jalna", "Maharashtra", "rolling mill", 30, "BILLET", 14, 120, "919900000104", 1),
        ("Mahakaushal Structurals", "Jabalpur", "Madhya Pradesh", "trader", 15, "SECTION", 12, 40, "919900000105", 2),
        ("Indore Fabricators & Traders", "Indore", "Madhya Pradesh", "fabricator", 0, "SECTION", 20, 25, "919900000106", 2),
        ("Shree Blast Tech", "Nagpur", "Maharashtra", "shot blasting", 30, "SHOTS", 21, 6, "919900000107", 3),
        ("Raigarh Foundry Works", "Raigarh", "Chhattisgarh", "foundry", 15, "SHOTS", 18, 4, "919900000108", 3),
        ("Korba Power Fabrication", "Korba", "Chhattisgarh", "fabricator", 0, "SECTION", 25, 15, "919900000109", 0),
        ("Bilaspur Alloys", "Bilaspur", "Chhattisgarh", "induction furnace", 15, "DRI", 10, 80, "919900000110", 0),
    ]
    t = today()
    for idx, (name, city, state, seg, credit, pcode, cycle, qty, phone, rep_i) in enumerate(customers):
        cust = Customer(name=name, city=city, state=state, segment=seg, credit_days=credit, phone=phone,
                        rep_id=reps[rep_i].id, credit_limit=qty * BASE_RATES[pcode] * 2, source="demo")
        db.add(cust)
        db.flush()
        db.add(Contact(customer_id=cust.id, name="Purchase Manager", phone=f"9198000{idx:05d}", role="purchase"))
        # Make a few customers lapse so the reorder module has something to find.
        stop_days_ago = {1: 16, 4: 40, 7: 50}.get(idx, rnd.randint(0, max(cycle - 1, 1)))
        d = t - timedelta(days=200)
        n = 0
        while d <= t - timedelta(days=stop_days_ago):
            q = round(qty * rnd.uniform(0.8, 1.2), 1)
            if idx == 9 and d > t - timedelta(days=30):
                q = round(qty * 0.4, 1)  # volume drop
            rate = BASE_RATES[pcode] + rnd.randint(-1500, 1500)
            n += 1
            order = Order(number=f"SO-D{idx:02d}{n:03d}", customer_id=cust.id, order_date=d,
                          product_id=products[pcode].id, item_text=products[pcode].name, qty_mt=q,
                          rate_per_mt=rate, amount=round(q * rate, 2), destination=city, status="delivered")
            db.add(order)
            db.flush()
            amount = round(q * rate * 1.18, 2)
            due = d + timedelta(days=credit)
            # Most invoices paid shortly after due; a few part-paid; Jalna (idx 3) badly overdue.
            if idx == 3 and 20 < (t - d).days < 75:
                paid = 0
            elif (t - due).days > 3:
                paid = amount if rnd.random() < 0.93 else amount * 0.5
            else:
                paid = 0
            db.add(Invoice(number=f"INV-D{idx:02d}{n:03d}", customer_id=cust.id, order_id=order.id,
                           invoice_date=d, due_date=due, amount=amount, paid_amount=round(paid, 2)))
            d += timedelta(days=max(1, int(rnd.gauss(cycle, cycle * 0.15))))

        last = db.scalar(select(Order).where(Order.customer_id == cust.id).order_by(Order.order_date.desc()))
        if last and (t - last.order_date).days <= 3:
            last.status = "dispatched"
            db.add(Dispatch(order_id=last.id, truck_no=f"CG04 MK {rnd.randint(1000, 9999)}",
                            driver_phone="919812345678", lr_no=f"LR{rnd.randint(10000, 99999)}",
                            qty_mt=last.qty_mt, eta=t + timedelta(days=1)))

    # Today's rates (the sales head would enter these each morning)
    for code, base in BASE_RATES.items():
        db.add(DailyRate(product_id=products[code].id, rate_date=t, base_rate_per_mt=base, entered_by="demo"))

    # Market prices (staff enter from their subscribed source)
    for i in range(14, -1, -1):
        d = t - timedelta(days=i)
        drift = (14 - i) * 40
        db.add(MarketPrice(price_date=d, item="Sponge iron (lumps), Raipur", price=27000 + drift + rnd.randint(-200, 200), source="demo"))
        db.add(MarketPrice(price_date=d, item="MS billet, Raipur", price=40000 + drift + rnd.randint(-300, 300), source="demo"))
        db.add(MarketPrice(price_date=d, item="Iron ore fines 63% Fe, Odisha", price=4200 + rnd.randint(-100, 100), source="demo"))

    # A few inbound leads
    for name, company, city, state, pcode, qty, src in [
        ("Ravi", "Ravi Steels", "Durg", "Chhattisgarh", "DRI", 60, "web"),
        ("Mahesh", "Sai TMT", "Wardha", "Maharashtra", "BILLET", 200, "portal"),
        ("Asif", "", "Hyderabad", "Telangana", "SHOTS", 2, "web"),
    ]:
        lead = Lead(source=src, name=name, company=company, city=city, state=state,
                    phone=f"91970000{rnd.randint(1000, 9999)}", product_id=products[pcode].id, qty_mt=qty,
                    status="qualified", frequency="monthly" if qty > 50 else "", created_at=now() - timedelta(hours=10))
        db.add(lead)
    db.flush()
    from app.services.crm import assign_rep, score_lead
    for lead in db.scalars(select(Lead)):
        lead.score = score_lead(lead)
        rep = assign_rep(db, lead)
        lead.rep_id = rep.id if rep else None

    for row in [
        dict(company="UltraCem Raipur Works", segment="cement", city="Raipur", state="Chhattisgarh",
             contact_name="Plant Head", designation="Plant Head", est_annual_qty_mt=600, current_supplier="Imported (China)"),
        dict(company="Baloda Bazar Cement Plant", segment="cement", city="Baloda Bazar", state="Chhattisgarh",
             contact_name="Mechanical HOD", designation="Head - Mechanical", est_annual_qty_mt=450),
        dict(company="Dalli Rajhara Iron Ore Mines", segment="mining", city="Balod", state="Chhattisgarh",
             contact_name="Maintenance Manager", designation="Maintenance", est_annual_qty_mt=40),
        dict(company="Korba Coalfields Contractor", segment="mining", city="Korba", state="Chhattisgarh",
             est_annual_qty_mt=25, contact_name="Purchase Officer", designation="Purchase"),
        dict(company="Satna Cement Works", segment="cement", city="Satna", state="Madhya Pradesh",
             est_annual_qty_mt=500, contact_name="Purchase Head", designation="Purchase"),
    ]:
        p = pipeline.add_prospect(db, row, actor="demo")
        p.rep_id = reps[3].id
    db.flush()

    # A ready-made customer portal login for trying the buyer side.
    from app.services import portal
    urla = db.scalar(select(Customer).where(Customer.name == "Urla Ispat Pvt Ltd"))
    user = portal.invite(db, urla, DEMO_LOGIN[0], "Purchase Manager")
    portal.activate(db, user.invite_token, DEMO_LOGIN[1])
    print(f"demo data loaded. Customer portal login: {DEMO_LOGIN[0]} / {DEMO_LOGIN[1]}")


def main() -> None:
    init_db()
    with session_scope() as db:
        seed_catalogue(db)
        if "--demo" in sys.argv:
            seed_demo(db)
    print("seed complete")


if __name__ == "__main__":
    main()
