"""Buyer-facing website: public home page with AI chat, the web chat API, and the customer portal."""

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from app.agent import runner
from app.db import get_db, session_scope
from app.models import ChatMessage, Conversation, Dispatch, DocumentScan, Notification, Order, PortalUser, Product, Quote
from app.services import approvals, documents, portal, receivables
from app.services.orders import recent_orders
from app.util import now
from app.web.common import attachment_links, back, get_or_404, rate_limited, render

router = APIRouter()
BUSY = "You're sending messages quickly. Please call 0771-264984 or try again in a few minutes."


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _history(db: Session, conv: Conversation | None, after: int) -> list[dict]:
    if conv is None:
        return []
    rows = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conv.id, ChatMessage.id > after)
                      .order_by(ChatMessage.id)).all()
    return [{"id": m.id, "from": "me" if m.direction == "in" else ("ai" if m.author == "ai" else "staff"),
             "text": m.body, "at": m.created_at.strftime("%d %b %H:%M")} for m in rows]


# ---------------------------------------------------------------- public pages

@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    if request.session.get("uid"):
        return back("/portal")
    products = db.scalars(select(Product).order_by(Product.id)).all()
    return render(request, "portal/landing.html", products=products)


# ---------------------------------------------------------------- anonymous web chat (home page + widget)

@router.post("/api/webchat")
async def webchat(request: Request):
    data = await request.json()
    session_id = str(data.get("session_id", ""))[:64]
    text = str(data.get("message", "")).strip()[:1000]
    if len(session_id) < 12 or not text:
        raise HTTPException(400, "session_id and message required")
    if rate_limited(f"ip:{_client_ip(request)}") or rate_limited(f"s:{session_id}"):
        return JSONResponse({"reply": BUSY, "files": [], "last_id": 0})
    with session_scope() as db:
        reply = runner.handle_inbound(db, "web", session_id, text)
        files = attachment_links(db, reply.attachments, portal=False, session_id=session_id)
    return {"reply": reply.text, "files": files, "last_id": reply.message_id}


@router.get("/api/webchat/history")
def webchat_history(session_id: str, after: int = 0, db: Session = Depends(get_db)):
    conv = db.scalar(select(Conversation).where(Conversation.channel == "web", Conversation.external_user == session_id))
    return {"messages": _history(db, conv, after), "human": bool(conv and conv.human_takeover)}


@router.get("/api/webchat/quote/{number}")
def webchat_quote(number: str, s: str, db: Session = Depends(get_db)):
    quote = db.scalar(select(Quote).where(Quote.number == number))
    conv = db.scalar(select(Conversation).where(Conversation.channel == "web", Conversation.external_user == s))
    if not quote or not conv or not conv.lead_id or quote.lead_id != conv.lead_id or not quote.pdf_path:
        raise HTTPException(404)
    return FileResponse(quote.pdf_path, media_type="application/pdf", filename=f"{number}.pdf")


# ---------------------------------------------------------------- portal auth

class LoginRequired(Exception):
    pass


def current_buyer(request: Request, db: Session = Depends(get_db)) -> PortalUser:
    uid = request.session.get("uid")
    user = db.get(PortalUser, uid) if uid else None
    if user is None or not user.active:
        request.session.clear()
        raise LoginRequired()
    return user


@router.get("/portal/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/portal"):
    return render(request, "portal/login.html", error="", next=next)


@router.post("/portal/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/portal"),
          db: Session = Depends(get_db)):
    if rate_limited(f"login:{_client_ip(request)}", limit=10, window=600):
        return render(request, "portal/login.html", error="Too many attempts. Try again in 10 minutes.", next=next)
    user = portal.authenticate(db, email, password)
    if user is None:
        return render(request, "portal/login.html", error="Wrong email or password.", next=next)
    request.session.clear()
    request.session["uid"] = user.id
    return back(next if next.startswith("/portal") else "/portal")


@router.post("/portal/logout")
def logout(request: Request):
    request.session.clear()
    return back("/")


@router.get("/portal/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render(request, "portal/register.html", error="", form={})


@router.post("/portal/register")
def register(request: Request, name: str = Form(...), company: str = Form(""), email: str = Form(...),
             phone: str = Form(""), city: str = Form(""), state: str = Form(""), password: str = Form(...),
             db: Session = Depends(get_db)):
    form = dict(name=name, company=company, email=email, phone=phone, city=city, state=state)
    if rate_limited(f"register:{_client_ip(request)}", limit=5, window=3600):
        return render(request, "portal/register.html", error="Too many sign-ups from this network. Try later.", form=form)
    try:
        user = portal.register(db, name, company, email, phone, password, city, state)
    except portal.PortalError as e:
        return render(request, "portal/register.html", error=str(e), form=form)
    request.session.clear()
    request.session["uid"] = user.id
    return back("/portal")


@router.get("/portal/activate/{token}", response_class=HTMLResponse)
def activate_page(token: str, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(select(PortalUser).where(PortalUser.invite_token == token))
    return render(request, "portal/activate.html", user=user, token=token, error="")


@router.post("/portal/activate/{token}")
def activate(token: str, request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    try:
        user = portal.activate(db, token, password)
    except portal.PortalError as e:
        user = db.scalar(select(PortalUser).where(PortalUser.invite_token == token))
        return render(request, "portal/activate.html", user=user, token=token, error=str(e))
    request.session.clear()
    request.session["uid"] = user.id
    return back("/portal")


# ---------------------------------------------------------------- portal pages

def _portal_conv(db: Session, user: PortalUser) -> Conversation | None:
    return db.scalar(select(Conversation).where(Conversation.channel == "portal",
                                                Conversation.external_user == f"user:{user.id}"))


def _visible_quotes(user: PortalUser):
    conds = []
    if user.customer_id:
        conds.append(Quote.customer_id == user.customer_id)
    if user.lead_id:
        conds.append(Quote.lead_id == user.lead_id)
    conv_leads = select(Conversation.lead_id).where(Conversation.channel == "portal",
                                                    Conversation.external_user == f"user:{user.id}")
    conds.append(Quote.lead_id.in_(conv_leads))
    return select(Quote).where(or_(*conds))


def _visible_notifications(user: PortalUser):
    conds = []
    if user.customer_id:
        conds.append(Notification.customer_id == user.customer_id)
    if user.lead_id:
        conds.append(Notification.lead_id == user.lead_id)
    return select(Notification).where(or_(*conds) if conds else false())


def _unread(db: Session, user: PortalUser) -> int:
    return len(db.scalars(_visible_notifications(user).where(Notification.read_at.is_(None))).all())


def page(request: Request, db: Session, user: PortalUser, name: str, **ctx):
    return render(request, name, user=user, unread=_unread(db, user), **ctx)


@router.get("/portal", response_class=HTMLResponse)
def portal_home(request: Request, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    orders = recent_orders(db, user.customer_id, 3) if user.customer_id else []
    outstanding = sum(i.outstanding for i in receivables.open_invoices(db, user.customer_id)) if user.customer_id else 0
    quotes = db.scalars(_visible_quotes(user).order_by(Quote.id.desc()).limit(3)).all()
    return page(request, db, user, "portal/home.html", orders=orders, outstanding=outstanding, quotes=quotes)


@router.post("/portal/chat")
async def portal_chat(request: Request, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    data = await request.json()
    text = str(data.get("message", "")).strip()[:1000]
    if not text:
        raise HTTPException(400)
    if rate_limited(f"pu:{user.id}", limit=40):
        return {"reply": BUSY, "files": [], "last_id": 0}
    reply = runner.handle_inbound(db, "portal", f"user:{user.id}", text, customer=user.customer,
                                  lead=user.lead, phone=user.phone)
    return {"reply": reply.text, "files": attachment_links(db, reply.attachments, portal=True),
            "last_id": reply.message_id}


@router.get("/portal/chat/history")
def portal_chat_history(after: int = 0, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    conv = _portal_conv(db, user)
    return {"messages": _history(db, conv, after), "human": bool(conv and conv.human_takeover)}


@router.get("/portal/orders", response_class=HTMLResponse)
def portal_orders(request: Request, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    orders = (db.scalars(select(Order).where(Order.customer_id == user.customer_id)
                         .order_by(Order.order_date.desc(), Order.id.desc()).limit(50)).all()
              if user.customer_id else [])
    return page(request, db, user, "portal/orders.html", orders=orders)


@router.get("/portal/account", response_class=HTMLResponse)
def portal_account(request: Request, error: str = "", uploaded: int = 0, user: PortalUser = Depends(current_buyer),
                   db: Session = Depends(get_db)):
    invoices = receivables.open_invoices(db, user.customer_id) if user.customer_id else []
    proofs = db.scalars(select(DocumentScan).where(DocumentScan.source == "portal",
                                                   DocumentScan.uploaded_by == user.email)
                        .order_by(DocumentScan.id.desc()).limit(10)).all()
    return page(request, db, user, "portal/account.html", invoices=invoices, proofs=proofs, error=error,
                uploaded=uploaded, total=sum(i.outstanding for i in invoices))


@router.post("/portal/payments/upload")
async def portal_payment_upload(request: Request, background: BackgroundTasks, file: UploadFile = File(...),
                                note: str = Form(""), user: PortalUser = Depends(current_buyer),
                                db: Session = Depends(get_db)):
    if rate_limited(f"upload:{user.id}", limit=10, window=3600):
        return back("/portal/account?error=Too+many+uploads.+Please+try+again+later.")
    try:
        scan = documents.save_upload(db, kind="payment", filename=file.filename or "", content=await file.read(),
                                     source="portal", uploaded_by=user.email, customer_id=user.customer_id,
                                     note=note)
    except ValueError as e:
        from urllib.parse import quote
        return back(f"/portal/account?error={quote(str(e))}")
    db.commit()
    background.add_task(documents.process_in_background, scan.id)
    return back("/portal/account?uploaded=1")


@router.get("/portal/quotes", response_class=HTMLResponse)
def portal_quotes(request: Request, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    quotes = db.scalars(_visible_quotes(user).order_by(Quote.id.desc()).limit(50)).all()
    return page(request, db, user, "portal/quotes.html", quotes=quotes)


def _own_quote(db: Session, user: PortalUser, number: str) -> Quote:
    quote = db.scalar(_visible_quotes(user).where(Quote.number == number))
    if quote is None:
        raise HTTPException(404)
    return quote


@router.get("/portal/quotes/{number}.pdf")
def portal_quote_pdf(number: str, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    quote = _own_quote(db, user, number)
    if quote.status in ("pending_approval", "rejected") or not quote.pdf_path:
        raise HTTPException(404)
    return FileResponse(quote.pdf_path, media_type="application/pdf", filename=f"{number}.pdf")


@router.post("/portal/quotes/{number}/accept")
def portal_quote_accept(number: str, note: str = Form(""), user: PortalUser = Depends(current_buyer),
                        db: Session = Depends(get_db)):
    approvals.buyer_accepts(db, _own_quote(db, user, number), note, via="portal")
    return back("/portal/quotes")


@router.get("/portal/mtc/{dispatch_id}")
def portal_mtc(dispatch_id: int, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    d = get_or_404(db, Dispatch, dispatch_id)
    if not user.customer_id or d.order.customer_id != user.customer_id or not d.mtc_path:
        raise HTTPException(404)
    return FileResponse(d.mtc_path, media_type="application/pdf", filename=f"MTC-{d.order.number}.pdf")


@router.get("/portal/inbox", response_class=HTMLResponse)
def portal_inbox(request: Request, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    items = db.scalars(_visible_notifications(user).order_by(Notification.id.desc()).limit(100)).all()
    resp = page(request, db, user, "portal/inbox.html", items=items)
    for n in items:  # opening the inbox marks everything read
        n.read_at = n.read_at or now()
    return resp


@router.get("/portal/inbox/{nid}/file")
def portal_inbox_file(nid: int, user: PortalUser = Depends(current_buyer), db: Session = Depends(get_db)):
    n = db.scalar(_visible_notifications(user).where(Notification.id == nid))
    if n is None or not n.attachment_path:
        raise HTTPException(404)
    return FileResponse(n.attachment_path, media_type="application/pdf")
