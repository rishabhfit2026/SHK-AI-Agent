"""New-product pipeline for the FY26-27 import-substitute lines (module 5)."""

import csv
import io
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product, Prospect
from app.services.crm import create_task
from app.services.pricing import find_product
from app.util import log_activity, norm_phone, today

STAGES = [
    "identified", "contacted", "sample_sent", "trial_running",
    "trial_passed", "approved_vendor", "first_order", "lost",
]
# Default follow-up gap after moving into a stage, and what to do next.
NEXT_STEP = {
    "identified": (2, "Make first contact: call or send intro message"),
    "contacted": (5, "Follow up: offer trial lot or plant visit"),
    "sample_sent": (7, "Confirm sample received; agree trial plan and success criteria"),
    "trial_running": (14, "Collect trial results (wear rate / life vs current supplier)"),
    "trial_passed": (5, "Push for vendor registration and commercial terms"),
    "approved_vendor": (7, "Chase first purchase order"),
    "first_order": (30, "Check performance; ask for annual rate contract"),
    "lost": (90, "Re-check in a quarter"),
}

# Which product each buying segment is most likely to need.
SEGMENT_PRODUCTS = {
    "cement": "GMB",
    "mining": "TOOTH",
    "power": "GMB",
    "earthmoving": "TOOTH",
    "steel": "CAST",
}


def set_stage(db: Session, prospect: Prospect, stage: str, actor: str = "system", note: str = "") -> None:
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage}")
    old = prospect.stage
    prospect.stage = stage
    gap, action = NEXT_STEP[stage]
    prospect.next_action = action
    prospect.next_action_date = today() + timedelta(days=gap)
    log_activity(db, "prospect", prospect.id, "stage", f"{old} → {stage}" + (f": {note}" if note else ""), actor)
    db.flush()


def add_prospect(db: Session, data: dict, actor: str = "system") -> Prospect:
    product = None
    if data.get("product"):
        product = find_product(db, str(data["product"]))
    elif data.get("segment") in SEGMENT_PRODUCTS:
        product = db.scalar(select(Product).where(Product.code == SEGMENT_PRODUCTS[data["segment"]]))
    p = Prospect(
        company=data["company"].strip(),
        segment=(data.get("segment") or "").strip().lower(),
        product_id=product.id if product else None,
        city=data.get("city", ""),
        state=data.get("state", ""),
        contact_name=data.get("contact_name", ""),
        designation=data.get("designation", ""),
        phone=norm_phone(data.get("phone", "")),
        email=data.get("email", ""),
        current_supplier=data.get("current_supplier", ""),
        est_annual_qty_mt=float(data["est_annual_qty_mt"]) if data.get("est_annual_qty_mt") else None,
        notes=data.get("notes", ""),
    )
    db.add(p)
    db.flush()
    set_stage(db, p, "identified", actor)
    return p


def import_prospects_csv(db: Session, text: str) -> int:
    """Columns: company, segment, product, city, state, contact_name, designation, phone, email,
    current_supplier, est_annual_qty_mt, notes. Rows with an existing company name are skipped."""
    existing = {c.lower() for c in db.scalars(select(Prospect.company))}
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("company") or row["company"].lower() in existing:
            continue
        add_prospect(db, row, actor="import")
        existing.add(row["company"].lower())
        n += 1
    return n


def due_actions(db: Session, on: date | None = None) -> list[Prospect]:
    on = on or today()
    return list(
        db.scalars(
            select(Prospect)
            .where(Prospect.stage.notin_(["lost"]), Prospect.next_action_date <= on)
            .order_by(Prospect.next_action_date)
        )
    )


def run_pipeline_followups(db: Session, on: date | None = None) -> dict:
    """Daily job: a task for each prospect whose next action is due."""
    on = on or today()
    n = 0
    for p in due_actions(db, on):
        if create_task(
            db, kind="pipeline", title=f"{p.company} ({p.stage.replace('_', ' ')}): {p.next_action}",
            dedupe_key=f"pipe:{p.id}:{p.stage}:{p.next_action_date}", prospect_id=p.id, rep_id=p.rep_id, due_date=on,
        ):
            n += 1
    return {"pipeline_tasks": n}


def funnel(db: Session) -> dict[str, int]:
    counts = {s: 0 for s in STAGES}
    for (stage,) in db.execute(select(Prospect.stage)):
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def draft_outreach(db: Session, prospect: Prospect, purpose: str = "", client=None) -> str:
    """The LLM drafts a message for this prospect; it lands in the Outbox for approval."""
    from app.agent import llm
    from app.agent.prompts import OUTREACH_SYSTEM
    from app.models import Outbox

    brief = {
        "company": prospect.company, "segment": prospect.segment,
        "product": prospect.product.name if prospect.product else "",
        "product_spec": prospect.product.spec if prospect.product else "",
        "contact": f"{prospect.contact_name} ({prospect.designation})".strip(" ()"),
        "location": ", ".join(x for x in (prospect.city, prospect.state) if x),
        "current_supplier": prospect.current_supplier, "stage": prospect.stage,
        "purpose": purpose or prospect.next_action, "notes": prospect.notes,
    }
    prompt = "Draft one short email (max 120 words) for this prospect:\n" + "\n".join(
        f"{k}: {v}" for k, v in brief.items() if v
    )
    body = llm.complete_text(OUTREACH_SYSTEM, prompt, effort="low", client=client)
    if prospect.email:
        db.add(Outbox(to_email=prospect.email, to_phone=prospect.phone, to_name=prospect.company, body=body,
                      kind="outreach"))
    log_activity(db, "prospect", prospect.id, "draft", body)
    db.flush()
    return body
