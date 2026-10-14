"""Shared helpers for the web routers."""

import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Dispatch, Quote
from app.services.orders import STATUS_TEXT
from app.util import inr, today

APP_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["inr"] = inr
templates.env.globals["STATUS_TEXT"] = STATUS_TEXT


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"today": today(), "settings": settings, **ctx})


def get_or_404(db: Session, model, key):
    obj = db.get(model, key)
    if obj is None:
        raise HTTPException(404)
    return obj


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


_hits: dict[str, deque] = defaultdict(deque)


def rate_limited(key: str, limit: int = 20, window: int = 600) -> bool:
    """Simple in-memory sliding window; enough for one server process."""
    q, t = _hits[key], time.time()
    while q and t - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


def attachment_links(db: Session, paths: list[Path], *, portal: bool, session_id: str = "") -> list[dict]:
    """Turn files the agent attached (quote PDFs, test certificates) into download links for the chat."""
    links = []
    for path in paths:
        quote = db.scalar(select(Quote).where(Quote.pdf_path == str(path)))
        if quote:
            url = (f"/portal/quotes/{quote.number}.pdf" if portal
                   else f"/api/webchat/quote/{quote.number}?s={session_id}")
            links.append({"url": url, "label": f"Quotation {quote.number} (PDF)"})
            continue
        dispatch = db.scalar(select(Dispatch).where(Dispatch.mtc_path == str(path)))
        if dispatch and portal:
            links.append({"url": f"/portal/mtc/{dispatch.id}",
                          "label": f"Test certificate, {dispatch.order.number} truck {dispatch.truck_no}"})
    return links
