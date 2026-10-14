"""Staff dashboard at /staff (HTTP basic auth)."""

import secrets
import time
from datetime import date, timedelta
from urllib.parse import quote as urlquote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import scheduler
from app.agent import runner
from app.config import settings
from app.db import get_db
from app.models import (
    Activity, Brief, ChatMessage, Conversation, Customer, DailyRate, Dispatch, DocumentScan, FreightRate, Lead,
    MarketPrice, Order,
    Outbox, PortalUser, Product, Prospect, Quote, SalesRep, Task,
)
from app.services import approvals, brief, documents, pipeline, portal, receivables, reorder, tally_import
from app.services.orders import order_from_quote
from app.util import log_activity, norm_phone, now, today
from app.web.common import back, get_or_404, render

security = HTTPBasic()


def current_user(creds: HTTPBasicCredentials = Depends(security)) -> str:
    ok_user = secrets.compare_digest(creds.username.encode(), settings.dashboard_user.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), settings.dashboard_password.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})
    return creds.username


router = APIRouter(prefix="/staff", dependencies=[Depends(current_user)])



@router.get("", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    latest = db.scalar(select(Brief).order_by(Brief.brief_date.desc()))
    metrics = brief.compute_metrics(db)
    tasks = db.scalars(select(Task).where(Task.status == "open").order_by(Task.due_date.is_(None), Task.due_date, Task.id.desc()).limit(25)).all()
    pending_out = db.scalar(select(func.count()).select_from(Outbox).where(Outbox.status == "pending_approval"))
    return render(request, "staff/overview.html", brief=latest, m=metrics, tasks=tasks, pending_out=pending_out)


@router.post("/jobs/{name}")
def run_job(name: str):
    if name not in scheduler.JOBS:
        raise HTTPException(404)
    scheduler.run_now(name)
    return back("/staff")


@router.post("/tasks/{task_id}/{action}")
def task_action(task_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    task = get_or_404(db, Task, task_id)
    task.status = {"done": "done", "dismiss": "dismissed"}.get(action, task.status)
    return back(request.headers.get("referer", "/staff"))


def _link_portal_logins(db: Session, lead_id: int, customer_id: int) -> None:
    """A buyer who self-registered and has now become a customer gets full account access."""
    for user in db.scalars(select(PortalUser).where(PortalUser.lead_id == lead_id, PortalUser.customer_id.is_(None))):
        user.customer_id = customer_id


# ---- leads

@router.get("/leads", response_class=HTMLResponse)
def leads(request: Request, status: str = "", db: Session = Depends(get_db)):
    q = select(Lead).order_by(Lead.id.desc()).limit(200)
    if status:
        q = q.where(Lead.status == status)
    return render(request, "staff/leads.html", leads=db.scalars(q).all(), status=status)


@router.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_db)):
    lead = get_or_404(db, Lead, lead_id)
    acts = db.scalars(select(Activity).where(Activity.entity == "lead", Activity.entity_id == lead_id).order_by(Activity.id.desc())).all()
    convs = db.scalars(select(Conversation).where(Conversation.lead_id == lead_id)).all()
    quotes = db.scalars(select(Quote).where(Quote.lead_id == lead_id).order_by(Quote.id.desc())).all()
    reps = db.scalars(select(SalesRep).where(SalesRep.active.is_(True))).all()
    logins = db.scalars(select(PortalUser).where(PortalUser.lead_id == lead_id)).all()
    customers = db.scalars(select(Customer).order_by(Customer.name)).all() if logins else []
    return render(request, "staff/lead.html", lead=lead, acts=acts, convs=convs, quotes=quotes, reps=reps,
                  logins=logins, customers=customers)


@router.post("/leads/{lead_id}")
def lead_update(lead_id: int, status: str = Form(...), rep_id: int = Form(0), note: str = Form(""),
                lost_reason: str = Form(""), user: str = Depends(current_user), db: Session = Depends(get_db)):
    lead = get_or_404(db, Lead, lead_id)
    lead.status, lead.rep_id = status, rep_id or None
    if status == "lost":
        lead.lost_reason = lost_reason
    if status == "won" and not lead.customer_id:
        cust = Customer(name=lead.company or lead.name or lead.phone, phone=lead.phone, email=lead.email,
                        city=lead.city, state=lead.state, rep_id=lead.rep_id, source=lead.source)
        db.add(cust)
        db.flush()
        lead.customer_id = cust.id
        _link_portal_logins(db, lead.id, cust.id)
    if note:
        log_activity(db, "lead", lead.id, "note", note, user)
    return back(f"/staff/leads/{lead_id}")


# ---- quotes

@router.get("/quotes", response_class=HTMLResponse)
def quotes(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Quote).order_by(Quote.status != "pending_approval", Quote.id.desc()).limit(200)).all()
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    return render(request, "staff/quotes.html", quotes=rows, customers=customers)


@router.post("/quotes/{quote_id}/{action}")
def quote_action(quote_id: int, action: str, reason: str = Form(""), customer_id: int = Form(0),
                 user: str = Depends(current_user), db: Session = Depends(get_db)):
    quote = get_or_404(db, Quote, quote_id)
    if action == "approve":
        approvals.approve_quote(db, quote, user, reason)
    elif action == "reject":
        approvals.reject_quote(db, quote, user, reason)
    elif action == "order":
        cid = quote.customer_id or customer_id
        if not cid:
            lead = quote.lead
            cust = Customer(name=quote.party_name, phone=quote.party_phone, city=lead.city if lead else "",
                            state=lead.state if lead else "", rep_id=lead.rep_id if lead else None, source="quote")
            db.add(cust)
            db.flush()
            cid = cust.id
            if lead:
                lead.customer_id, lead.status = cid, "won"
                _link_portal_logins(db, lead.id, cid)
        quote.customer_id = cid
        order_from_quote(db, quote, cid)
        return back("/staff/orders")
    return back("/staff/quotes")


@router.get("/quotes/{quote_id}/pdf")
def quote_pdf(quote_id: int, db: Session = Depends(get_db)):
    quote = get_or_404(db, Quote, quote_id)
    if not quote or not quote.pdf_path:
        from app.services.quote_pdf import render_quote_pdf
        render_quote_pdf(db, quote)
    return FileResponse(quote.pdf_path, media_type="application/pdf", filename=f"{quote.number}.pdf")


# ---- customers

@router.get("/customers", response_class=HTMLResponse)
def customers(request: Request, db: Session = Depends(get_db)):
    cycles = reorder.all_cycles(db)
    dues = {r.customer.id: r for r in receivables.ageing(db)}
    cycles.sort(key=lambda c: (not c.lapsed, not c.volume_drop, c.customer.name))
    return render(request, "staff/customers.html", cycles=cycles, dues=dues)


@router.get("/customers/{cid}", response_class=HTMLResponse)
def customer_detail(cid: int, request: Request, invite: str = "", db: Session = Depends(get_db)):
    cust = get_or_404(db, Customer, cid)
    orders = db.scalars(select(Order).where(Order.customer_id == cid).order_by(Order.order_date.desc()).limit(30)).all()
    invs = receivables.open_invoices(db, cid)
    cyc = next((c for c in reorder.all_cycles(db) if c.customer.id == cid), None)
    tasks = db.scalars(select(Task).where(Task.customer_id == cid).order_by(Task.id.desc()).limit(20)).all()
    convs = db.scalars(select(Conversation).where(Conversation.customer_id == cid)).all()
    logins = db.scalars(select(PortalUser).where(PortalUser.customer_id == cid)).all()
    return render(request, "staff/customer.html", invite=invite, c=cust, orders=orders, invs=invs, cyc=cyc, tasks=tasks,
                  convs=convs, logins=logins)


@router.post("/customers/{cid}")
def customer_update(cid: int, phone: str = Form(""), email: str = Form(""), credit_days: int = Form(0),
                    segment: str = Form(""), db: Session = Depends(get_db)):
    cust = get_or_404(db, Customer, cid)
    cust.phone, cust.email, cust.credit_days, cust.segment = norm_phone(phone), email.strip(), credit_days, segment
    return back(f"/staff/customers/{cid}")


@router.post("/customers/{cid}/invite")
def customer_invite(cid: int, email: str = Form(...), name: str = Form(""), db: Session = Depends(get_db)):
    """Create a portal login for this customer and email the activation link."""
    cust = get_or_404(db, Customer, cid)
    user = portal.invite(db, cust, email, name)
    link = f"{settings.public_base_url}/portal/activate/{user.invite_token}"
    db.add(Outbox(to_email=user.email, to_name=cust.name, kind="portal_invite", status="approved",
                  decided_by="staff", subject="Your Shri Hare-Krishna Sponge Iron customer portal login",
                  body=(f"Dear {user.name or cust.name},\n\nYour customer portal account is ready. Set your password "
                        f"here:\n{link}\n\nIn the portal you can get today's rates, download quotations and test "
                        f"certificates, track dispatches and see your account statement.")))
    return back(f"/staff/customers/{cid}?invite={urlquote(link)}")


@router.post("/portal-users/{uid}/link")
def portal_user_link(uid: int, customer_id: int = Form(...), db: Session = Depends(get_db)):
    """Link a self-registered portal user to a customer account after checking who they are."""
    user = get_or_404(db, PortalUser, uid)
    user.customer_id = get_or_404(db, Customer, customer_id).id
    return back(f"/staff/customers/{customer_id}")


@router.post("/portal-users/{uid}/disable")
def portal_user_disable(uid: int, request: Request, db: Session = Depends(get_db)):
    get_or_404(db, PortalUser, uid).active = False
    return back(request.headers.get("referer", "/staff"))


# ---- orders & dispatch

@router.get("/orders", response_class=HTMLResponse)
def orders(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Order).where(Order.status != "delivered").order_by(Order.order_date.desc()).limit(200)).all()
    return render(request, "staff/orders.html", orders=rows)


@router.post("/orders/{oid}/status")
def order_status(oid: int, status: str = Form(...), db: Session = Depends(get_db)):
    order = get_or_404(db, Order, oid)
    order.status = status
    if status == "delivered":
        for d in order.dispatches:
            d.delivered_at = d.delivered_at or now()
    return back("/staff/orders")


@router.post("/orders/{oid}/dispatch")
async def order_dispatch(oid: int, truck_no: str = Form(...), driver_phone: str = Form(""), lr_no: str = Form(""),
                         qty_mt: float = Form(...), eta: str = Form(""), notify: bool = Form(False),
                         mtc: UploadFile | None = File(None), db: Session = Depends(get_db)):
    order = get_or_404(db, Order, oid)
    d = Dispatch(order_id=oid, truck_no=truck_no.upper(), driver_phone=norm_phone(driver_phone), lr_no=lr_no,
                 qty_mt=qty_mt, eta=date.fromisoformat(eta) if eta else None)
    if mtc and mtc.filename:
        folder = settings.data_dir / "mtc"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{order.number}-{int(time.time())}.pdf"
        path.write_bytes(await mtc.read())
        d.mtc_path = str(path)
    db.add(d)
    order.status = "dispatched"
    if notify:
        details = (f"Dispatch update for order {order.number}: {qty_mt:g} MT loaded on truck {d.truck_no}"
                   f"{', LR ' + lr_no if lr_no else ''}{', driver ' + d.driver_phone if d.driver_phone else ''}"
                   f"{', expected ' + eta if eta else ''}.")
        if d.mtc_path:
            details += " The mill test certificate is attached and available in your portal under Orders."
        # Staff triggered it themselves, so it goes straight out.
        db.add(Outbox(to_phone=order.customer.phone, to_name=order.customer.name, kind="dispatch",
                      customer_id=order.customer_id, attachment_path=d.mtc_path, status="approved",
                      decided_by="dispatch", body=details))
    return back("/staff/orders")


# ---- receivables

@router.get("/receivables", response_class=HTMLResponse)
def receivables_page(request: Request, db: Session = Depends(get_db)):
    rows = receivables.ageing(db)
    totals = {b: sum(r.buckets[b] for r in rows) for b in receivables.BUCKETS}
    return render(request, "staff/receivables.html", rows=rows, totals=totals, buckets=receivables.BUCKETS)


@router.post("/receivables/payment")
def add_payment(customer_id: int = Form(...), amount: float = Form(...), paid_on: str = Form(""),
                mode: str = Form(""), ref: str = Form(""), db: Session = Depends(get_db)):
    receivables.record_payment(db, customer_id, amount, date.fromisoformat(paid_on) if paid_on else today(), mode, ref)
    return back("/staff/receivables")


# ---- pipeline (new products)

@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_page(request: Request, db: Session = Depends(get_db)):
    prospects = db.scalars(select(Prospect).order_by(Prospect.next_action_date)).all()
    products = db.scalars(select(Product).where(Product.status == "upcoming")).all()
    return render(request, "staff/pipeline.html", prospects=prospects, funnel=pipeline.funnel(db),
                  stages=pipeline.STAGES, products=products)


@router.post("/pipeline")
def pipeline_add(company: str = Form(...), segment: str = Form(""), product: str = Form(""), city: str = Form(""),
                 state: str = Form(""), contact_name: str = Form(""), designation: str = Form(""),
                 phone: str = Form(""), est_annual_qty_mt: str = Form(""), user: str = Depends(current_user),
                 db: Session = Depends(get_db)):
    pipeline.add_prospect(db, dict(company=company, segment=segment, product=product, city=city, state=state,
                                   contact_name=contact_name, designation=designation, phone=phone,
                                   est_annual_qty_mt=est_annual_qty_mt), actor=user)
    return back("/staff/pipeline")


@router.post("/pipeline/import")
async def pipeline_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    pipeline.import_prospects_csv(db, (await file.read()).decode("utf-8", errors="replace"))
    return back("/staff/pipeline")


@router.get("/pipeline/{pid}", response_class=HTMLResponse)
def prospect_detail(pid: int, request: Request, db: Session = Depends(get_db)):
    p = get_or_404(db, Prospect, pid)
    acts = db.scalars(select(Activity).where(Activity.entity == "prospect", Activity.entity_id == pid).order_by(Activity.id.desc())).all()
    return render(request, "staff/prospect.html", p=p, acts=acts, stages=pipeline.STAGES)


@router.post("/pipeline/{pid}/stage")
def prospect_stage(pid: int, stage: str = Form(...), note: str = Form(""), user: str = Depends(current_user),
                   db: Session = Depends(get_db)):
    pipeline.set_stage(db, get_or_404(db, Prospect, pid), stage, user, note)
    return back(f"/staff/pipeline/{pid}")


@router.post("/pipeline/{pid}/draft")
def prospect_draft(pid: int, purpose: str = Form(""), db: Session = Depends(get_db)):
    from app.agent.llm import LLMUnavailable
    try:
        pipeline.draft_outreach(db, get_or_404(db, Prospect, pid), purpose)
    except LLMUnavailable as e:
        log_activity(db, "prospect", pid, "error", f"Draft failed: {e}")
    return back(f"/staff/pipeline/{pid}")


# ---- outbox

@router.get("/outbox", response_class=HTMLResponse)
def outbox(request: Request, db: Session = Depends(get_db)):
    pending = db.scalars(select(Outbox).where(Outbox.status == "pending_approval").order_by(Outbox.id)).all()
    recent = db.scalars(select(Outbox).where(Outbox.status != "pending_approval").order_by(Outbox.id.desc()).limit(50)).all()
    return render(request, "staff/outbox.html", pending=pending, recent=recent)


@router.post("/outbox/{oid}/{action}")
def outbox_action(oid: int, action: str, body: str = Form(None), user: str = Depends(current_user),
                  db: Session = Depends(get_db)):
    approvals.decide_outbox(db, get_or_404(db, Outbox, oid), action == "approve", user, body)
    return back("/staff/outbox")


# ---- rates & market prices

@router.get("/rates", response_class=HTMLResponse)
def rates(request: Request, db: Session = Depends(get_db)):
    products = db.scalars(select(Product).where(Product.status == "live")).all()
    todays = {r.product_id: r for r in db.scalars(select(DailyRate).where(DailyRate.rate_date == today()))}
    last = {}
    for p in products:
        last[p.id] = db.scalar(select(DailyRate).where(DailyRate.product_id == p.id, DailyRate.rate_date < today())
                               .order_by(DailyRate.rate_date.desc()))
    market = db.scalars(select(MarketPrice).where(MarketPrice.price_date >= today() - timedelta(days=7))
                        .order_by(MarketPrice.price_date.desc(), MarketPrice.item)).all()
    freight = db.scalars(select(FreightRate).order_by(FreightRate.state, FreightRate.city)).all()
    return render(request, "staff/rates.html", products=products, todays=todays, last=last, market=market, freight=freight)


@router.post("/rates")
async def rates_save(request: Request, user: str = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    for key, val in form.items():
        if not key.startswith("rate_") or not str(val).strip():
            continue
        pid = int(key.removeprefix("rate_"))
        row = db.scalar(select(DailyRate).where(DailyRate.product_id == pid, DailyRate.rate_date == today()))
        if row is None:
            row = DailyRate(product_id=pid, rate_date=today(), base_rate_per_mt=0)
            db.add(row)
        row.base_rate_per_mt, row.entered_by = float(val), user
    return back("/staff/rates")


@router.post("/rates/market")
def market_add(item: str = Form(...), price: float = Form(...), unit: str = Form("INR/MT"), source: str = Form(""),
               db: Session = Depends(get_db)):
    db.add(MarketPrice(price_date=today(), item=item.strip(), price=price, unit=unit, source=source))
    return back("/staff/rates")


@router.post("/rates/freight")
def freight_save(city: str = Form(...), state: str = Form(""), per_mt: float = Form(...), db: Session = Depends(get_db)):
    row = db.scalar(select(FreightRate).where(FreightRate.city == city.strip().lower()))
    if row is None:
        row = FreightRate(city=city.strip().lower(), state=state, per_mt=per_mt)
        db.add(row)
    row.per_mt, row.state = per_mt, state or row.state
    return back("/staff/rates")


# ---- conversations

@router.get("/conversations", response_class=HTMLResponse)
def conversations(request: Request, db: Session = Depends(get_db)):
    rows = db.scalars(select(Conversation).where(Conversation.channel != "staff")
                      .order_by(Conversation.last_inbound_at.desc().nulls_last()).limit(100)).all()
    return render(request, "staff/conversations.html", convs=rows)


@router.get("/conversations/{cid}", response_class=HTMLResponse)
def conversation_detail(cid: int, request: Request, db: Session = Depends(get_db)):
    return render(request, "staff/conversation.html", conv=get_or_404(db, Conversation, cid))


@router.post("/conversations/{cid}/takeover")
def conversation_takeover(cid: int, on: bool = Form(False), db: Session = Depends(get_db)):
    get_or_404(db, Conversation, cid).human_takeover = on
    return back(f"/staff/conversations/{cid}")


@router.post("/conversations/{cid}/reply")
def conversation_reply(cid: int, body: str = Form(...), user: str = Depends(current_user), db: Session = Depends(get_db)):
    get_or_404(db, Conversation, cid)
    # Shows up in the buyer's chat window (website widget and portal poll for new messages).
    db.add(ChatMessage(conversation_id=cid, direction="out", author=user, body=body))
    return back(f"/staff/conversations/{cid}")


# ---- simulator & staff assistant

@router.get("/simulator", response_class=HTMLResponse)
def simulator(request: Request, customer_id: int = 0, db: Session = Depends(get_db)):
    """Chat as a buyer would on the website: as an anonymous visitor, or as a logged-in customer."""
    key = f"sim:{customer_id}"
    channel = "portal" if customer_id else "web"
    conv = db.scalar(select(Conversation).where(Conversation.channel == channel, Conversation.external_user == key))
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    return render(request, "staff/simulator.html", customer_id=customer_id, conv=conv, customers=customers)


@router.post("/simulator")
def simulator_send(customer_id: int = Form(0), text: str = Form(...), db: Session = Depends(get_db)):
    customer = db.get(Customer, customer_id) if customer_id else None
    runner.handle_inbound(db, "portal" if customer else "web", f"sim:{customer_id}", text,
                          customer=customer, phone=customer.phone if customer else "")
    return back(f"/staff/simulator?customer_id={customer_id}")


@router.post("/simulator/reset")
def simulator_reset(customer_id: int = Form(0), db: Session = Depends(get_db)):
    conv = db.scalar(select(Conversation).where(Conversation.external_user == f"sim:{customer_id}"))
    if conv:
        db.delete(conv)
    return back(f"/staff/simulator?customer_id={customer_id}")


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, user: str = Depends(current_user), db: Session = Depends(get_db)):
    conv = db.scalar(select(Conversation).where(Conversation.channel == "staff", Conversation.external_user == user))
    return render(request, "staff/ask.html", conv=conv)


@router.post("/ask")
def ask_send(question: str = Form(...), user: str = Depends(current_user), db: Session = Depends(get_db)):
    runner.staff_ask(db, user, question)
    return back("/staff/ask")


@router.post("/ask/reset")
def ask_reset(user: str = Depends(current_user), db: Session = Depends(get_db)):
    conv = db.scalar(select(Conversation).where(Conversation.channel == "staff", Conversation.external_user == user))
    if conv:
        db.delete(conv)
    return back("/staff/ask")


# ---- documents (OCR: payment proofs and invoices)

@router.get("/documents", response_class=HTMLResponse)
def documents_page(request: Request, db: Session = Depends(get_db)):
    pending = db.scalars(select(DocumentScan).where(DocumentScan.status.in_(("processing", "needs_review")))
                         .order_by(DocumentScan.id)).all()
    recent = db.scalars(select(DocumentScan).where(DocumentScan.status.in_(("confirmed", "rejected")))
                        .order_by(DocumentScan.id.desc()).limit(30)).all()
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    return render(request, "staff/documents.html", pending=pending, recent=recent, customers=customers)


@router.post("/documents/upload")
async def documents_upload(background: BackgroundTasks, kind: str = Form(...), customer_id: int = Form(0),
                           file: UploadFile = File(...), user: str = Depends(current_user),
                           db: Session = Depends(get_db)):
    scan = documents.save_upload(db, kind=kind, filename=file.filename or "", content=await file.read(),
                                 source="staff", uploaded_by=user, customer_id=customer_id or None)
    db.commit()
    background.add_task(documents.process_in_background, scan.id)
    return back(f"/staff/documents/{scan.id}")


@router.get("/documents/{sid}", response_class=HTMLResponse)
def document_detail(sid: int, request: Request, db: Session = Depends(get_db)):
    scan = get_or_404(db, DocumentScan, sid)
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    invoices = receivables.open_invoices(db, scan.customer_id) if scan.customer_id else []
    return render(request, "staff/document.html", scan=scan, customers=customers, invoices=invoices)


@router.get("/documents/{sid}/file")
def document_file(sid: int, db: Session = Depends(get_db)):
    scan = get_or_404(db, DocumentScan, sid)
    media = "application/pdf" if scan.file_path.endswith(".pdf") else None
    return FileResponse(scan.file_path, media_type=media)


@router.post("/documents/{sid}/reprocess")
def document_reprocess(sid: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    scan = get_or_404(db, DocumentScan, sid)
    if scan.status in ("confirmed", "rejected"):
        raise ValueError(f"Document is already {scan.status}")
    scan.status, scan.error = "processing", ""
    db.commit()
    background.add_task(documents.process_in_background, sid)
    return back(f"/staff/documents/{sid}")


@router.post("/documents/{sid}/confirm-payment")
def document_confirm_payment(sid: int, customer_id: int = Form(...), amount: float = Form(...),
                             paid_on: str = Form(...), reference: str = Form(""), mode: str = Form(""),
                             invoice_id: int = Form(0), user: str = Depends(current_user),
                             db: Session = Depends(get_db)):
    scan = get_or_404(db, DocumentScan, sid)
    documents.confirm_payment(db, scan, customer_id=customer_id, amount=amount, paid_on=date.fromisoformat(paid_on),
                              reference=reference, mode=mode, invoice_id=invoice_id or None, user=user)
    return back("/staff/documents")


@router.post("/documents/{sid}/confirm-invoice")
def document_confirm_invoice(sid: int, number: str = Form(...), invoice_date: str = Form(...),
                             amount: float = Form(...), customer_id: int = Form(0), new_customer_name: str = Form(""),
                             gstin: str = Form(""), city: str = Form(""), state: str = Form(""),
                             due_date: str = Form(""), item: str = Form(""), qty_mt: str = Form(""),
                             rate_per_mt: str = Form(""), user: str = Depends(current_user),
                             db: Session = Depends(get_db)):
    scan = get_or_404(db, DocumentScan, sid)
    documents.confirm_invoice(
        db, scan, customer_id=customer_id or None, new_customer_name=new_customer_name, gstin=gstin, city=city,
        state=state, number=number, invoice_date=date.fromisoformat(invoice_date),
        due_date=date.fromisoformat(due_date) if due_date else None, amount=amount, item=item,
        qty_mt=float(qty_mt) if qty_mt else None, rate_per_mt=float(rate_per_mt) if rate_per_mt else None, user=user)
    return back("/staff/documents")


@router.post("/documents/{sid}/reject")
def document_reject(sid: int, reason: str = Form(""), user: str = Depends(current_user), db: Session = Depends(get_db)):
    documents.reject(db, get_or_404(db, DocumentScan, sid), user, reason)
    return back("/staff/documents")


# ---- imports & brief

@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, result: str = ""):
    return render(request, "staff/import.html", result=result)


@router.post("/import/{kind}")
async def import_upload(kind: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    text = (await file.read()).decode("utf-8", errors="replace")
    if kind == "sales":
        res = tally_import.import_sales(db, text)
    elif kind == "receipts":
        res = tally_import.import_receipts(db, text)
    else:
        raise HTTPException(404)
    errs = res.pop("errors", [])
    summary = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in res.items())
    if errs:
        summary += f" | {len(errs)} errors: " + "; ".join(errs[:5])
    return back(f"/staff/import?result={urlquote(summary)}")


@router.post("/brief/generate")
def brief_generate(db: Session = Depends(get_db)):
    brief.generate_brief(db, send=False)
    return back("/staff")

