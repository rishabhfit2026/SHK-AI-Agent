"""FastAPI app.

  /                 public website: products + AI sales chat for anonymous visitors
  /portal/...       customer portal (login): chat, orders, dispatches, certificates, invoices, quotes, inbox
  /staff/...        staff dashboard (HTTP basic auth)
  /static/widget.js chat widget to embed on shkraipur.com
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import scheduler
from app.config import settings
from app.db import init_db
from app.web import public, staff
from app.web.common import APP_DIR, back

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sched = scheduler.start() if not app.state.testing else None
    yield
    if sched:
        sched.shutdown(wait=False)


app = FastAPI(title="SHK Sales Desk", lifespan=lifespan)
app.state.testing = False
# Only the anonymous chat API is called cross-site (from the widget on shkraipur.com); no cookies involved.
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST"],
                   allow_headers=["Content-Type"])
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="shk_portal",
                   max_age=14 * 24 * 3600, same_site="lax", https_only=settings.public_base_url.startswith("https"))
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
app.include_router(public.router)
app.include_router(staff.router)


@app.get("/healthz")
def health():
    return {"ok": True, "llm": settings.llm_enabled, "email": settings.email_enabled}


@app.exception_handler(public.LoginRequired)
def login_required(request: Request, exc: public.LoginRequired):
    if request.method != "GET" or request.url.path.startswith(("/portal/chat", "/api/")):
        return JSONResponse({"error": "login required"}, status_code=401)
    return back(f"/portal/login?next={request.url.path}")


@app.exception_handler(ValueError)
def value_error(request: Request, exc: ValueError):
    return PlainTextResponse(f"Not allowed: {exc}", status_code=400)
