from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

import app.db as app_db
from app.agent import llm
from app.main import app
from app.models import Conversation, Customer, Dispatch, Notification, Order, Outbox, PortalUser, Quote, Task
from app.services import notify, portal
from app.util import today
from tests.conftest import FakeClient, add_invoice, add_orders, text_msg, tool_msg

AUTH = ("admin", "change-me")


@pytest.fixture
def client(db, monkeypatch):
    db.commit()
    monkeypatch.setattr(app_db, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    app.state.testing = True
    with TestClient(app) as c:
        yield c


def use_fake_llm(monkeypatch, *responses) -> FakeClient:
    fake = FakeClient(*responses)
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    monkeypatch.setattr(type(llm.settings), "llm_enabled", property(lambda self: True))
    return fake


def make_login(db, customer, email="buyer@example.com", password="secret-pass-1") -> PortalUser:
    user = portal.invite(db, customer, email)
    portal.activate(db, user.invite_token, password)
    db.commit()
    return user


def login(client, email="buyer@example.com", password="secret-pass-1"):
    r = client.post("/portal/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portal"


# ---------------------------------------------------------------- staff

def test_staff_requires_login(client):
    assert client.get("/staff").status_code == 401
    assert client.get("/staff", auth=AUTH).status_code == 200


@pytest.mark.parametrize("path", ["/staff", "/staff/leads", "/staff/quotes", "/staff/customers", "/staff/orders",
                                  "/staff/receivables", "/staff/pipeline", "/staff/outbox", "/staff/conversations",
                                  "/staff/rates", "/staff/ask", "/staff/simulator", "/staff/import"])
def test_staff_pages_render(client, path):
    assert client.get(path, auth=AUTH).status_code == 200


def test_staff_invite_creates_activation_email(client, db, customer):
    r = client.post(f"/staff/customers/{customer.id}/invite", data={"email": "Purchase@Urla.example"}, auth=AUTH,
                    follow_redirects=False)
    assert r.status_code == 303 and "/portal/activate/" in r.headers["location"]
    user = db.scalar(select(PortalUser))
    assert user.email == "purchase@urla.example" and user.customer_id == customer.id and user.invite_token
    out = db.scalar(select(Outbox).where(Outbox.kind == "portal_invite"))
    assert out.status == "approved" and user.invite_token in out.body
    assert client.get(f"/staff/customers/{customer.id}", auth=AUTH).status_code == 200


# ---------------------------------------------------------------- public site + anonymous chat

def test_public_home_and_anonymous_chat(client, monkeypatch, db):
    assert "Get today's rate" in client.get("/").text
    use_fake_llm(
        monkeypatch,
        tool_msg(("get_price_quote", {"items": [{"product": "steel shots", "variant": "Shot S-330", "qty_mt": 2}],
                                      "destination": "Raipur"})),
        text_msg("₹68,700/MT + GST delivered Raipur. Quotation attached."),
    )
    sid = "sess-abcdef-123"
    r = client.post("/api/webchat", json={"session_id": sid, "message": "2 MT S-330 Raipur"}).json()
    assert r["reply"].startswith("₹68,700") and r["last_id"] > 0
    pdf = r["files"][0]["url"]
    assert client.get(pdf).headers["content-type"] == "application/pdf"
    assert client.get(pdf.replace(sid, "someone-elses-sess")).status_code == 404

    # Staff reply typed in the dashboard reaches the visitor via polling.
    conv_id = db.scalar(select(Conversation.id).where(Conversation.external_user == sid))
    client.post(f"/staff/conversations/{conv_id}/reply", data={"body": "Sir, I'll call you now."}, auth=AUTH)
    hist = client.get("/api/webchat/history", params={"session_id": sid, "after": r["last_id"]}).json()
    assert [(m["from"], m["text"]) for m in hist["messages"]] == [("staff", "Sir, I'll call you now.")]


def test_webchat_validates_input(client):
    assert client.post("/api/webchat", json={"session_id": "short", "message": "hi"}).status_code == 400


# ---------------------------------------------------------------- portal

def test_portal_pages_need_login(client):
    r = client.get("/portal/orders", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/portal/login")
    assert client.post("/portal/chat", json={"message": "hi"}).status_code == 401


def test_register_then_chat_as_new_buyer(client, monkeypatch, db, customer):
    r = client.post("/portal/register", data={"name": "Ravi", "company": "Ravi Steels", "email": "ravi@example.com",
                                              "phone": customer.phone, "password": "longenough1"},
                    follow_redirects=False)
    assert r.status_code == 303
    user = db.scalar(select(PortalUser).where(PortalUser.email == "ravi@example.com"))
    # Typing an existing customer's phone at sign-up must not grant access to that account.
    assert user.customer_id is None and user.lead_id
    assert "isn't linked" in client.get("/portal/orders").text

    fake = use_fake_llm(monkeypatch, text_msg("Namaste Ravi! Which product do you need?"))
    r = client.post("/portal/chat", json={"message": "hello"}).json()
    assert r["reply"].startswith("Namaste Ravi")
    assert "not yet linked to a customer account" in fake.requests[0]["messages"][2]["content"]
    assert client.get("/portal/chat/history").json()["messages"][0]["text"] == "hello"


def test_register_rejects_duplicates_and_weak_passwords(client, db):
    data = {"name": "A", "email": "a@example.com", "password": "short"}
    assert "at least 8" in client.post("/portal/register", data=data).text
    data["password"] = "long-enough"
    client.post("/portal/register", data=data, follow_redirects=False)
    client.post("/portal/logout")
    assert "already exists" in client.post("/portal/register", data=data).text


def test_wrong_password_rejected(client, db, customer):
    make_login(db, customer)
    assert "Wrong email or password" in client.post("/portal/login", data={"email": "buyer@example.com",
                                                                            "password": "nope-nope"}).text


def test_activation_link_sets_password_once(client, db, customer):
    user = portal.invite(db, customer, "new@urla.example")
    db.commit()
    token = user.invite_token
    assert "Set your password" in client.get(f"/portal/activate/{token}").text
    r = client.post(f"/portal/activate/{token}", data={"password": "brand-new-pass"}, follow_redirects=False)
    assert r.headers["location"] == "/portal"
    assert "expired" in client.get(f"/portal/activate/{token}").text


def test_customer_sees_own_orders_invoices_and_certificates_only(client, db, customer, tmp_path):
    other = Customer(name="Other Co", phone="919911111111")
    db.add(other)
    db.flush()
    add_orders(db, customer, [3])
    add_orders(db, other, [2])
    add_invoice(db, customer, "INV-MINE", 5, 118000)
    add_invoice(db, other, "INV-THEIRS", 5, 50000)
    mine, theirs = (db.scalar(select(Order).where(Order.customer_id == c.id)) for c in (customer, other))
    cert = tmp_path / "mtc.pdf"
    cert.write_bytes(b"%PDF-1.4 test")
    d_mine = Dispatch(order_id=mine.id, truck_no="CG04AB1234", qty_mt=10, mtc_path=str(cert))
    d_theirs = Dispatch(order_id=theirs.id, truck_no="CG04ZZ9999", qty_mt=10, mtc_path=str(cert))
    db.add_all([d_mine, d_theirs])
    make_login(db, customer)
    login(client)

    orders = client.get("/portal/orders").text
    assert mine.number in orders and theirs.number not in orders
    account = client.get("/portal/account").text
    assert "INV-MINE" in account and "INV-THEIRS" not in account
    assert client.get(f"/portal/mtc/{d_mine.id}").status_code == 200
    assert client.get(f"/portal/mtc/{d_theirs.id}").status_code == 404


def test_portal_chat_gives_account_answers_and_cert_links(client, monkeypatch, db, customer, tmp_path):
    add_orders(db, customer, [1])
    order = db.scalar(select(Order))
    cert = tmp_path / "mtc.pdf"
    cert.write_bytes(b"%PDF-1.4 test")
    db.add(Dispatch(order_id=order.id, truck_no="CG04AB1234", qty_mt=10, mtc_path=str(cert)))
    make_login(db, customer)
    login(client)
    use_fake_llm(monkeypatch, tool_msg(("send_test_certificate", {"order_number": order.number})),
                 text_msg("Here is your test certificate."))
    r = client.post("/portal/chat", json={"message": f"MTC for {order.number}"}).json()
    assert r["files"][0]["url"].startswith("/portal/mtc/")
    assert client.get(r["files"][0]["url"]).status_code == 200


def test_quote_approval_reaches_portal_inbox_and_buyer_can_accept(client, monkeypatch, db, customer, rep):
    make_login(db, customer)
    login(client)
    use_fake_llm(monkeypatch,
                 tool_msg(("get_price_quote", {"items": [{"product": "billet", "qty_mt": 500}], "destination": "Nagpur"})),
                 text_msg("Our team will confirm the rate shortly."))
    client.post("/portal/chat", json={"message": "500 MT billets Nagpur"})
    quote = db.scalar(select(Quote))
    assert quote.status == "pending_approval" and quote.customer_id == customer.id
    page = client.get("/portal/quotes").text
    assert "being confirmed" in page and "₹" not in page.split(quote.number)[1].split("</tr>")[0]
    assert client.get(f"/portal/quotes/{quote.number}.pdf").status_code == 404

    ob = db.scalar(select(Outbox).where(Outbox.quote_id == quote.id))
    client.post(f"/staff/outbox/{ob.id}/approve", data={"body": ob.body}, auth=AUTH)
    client.post("/staff/jobs/flush_outbox", auth=AUTH)
    db.expire_all()
    assert db.scalar(select(Outbox).where(Outbox.quote_id == quote.id)).status == "sent"
    note = db.scalar(select(Notification).where(Notification.quote_id == quote.id))
    assert note.customer_id == customer.id
    assert "<span class=\"badge\">1</span>" in client.get("/portal").text
    assert quote.number in client.get("/portal/inbox").text
    assert client.get(f"/portal/inbox/{note.id}/file").status_code == 200

    assert client.get(f"/portal/quotes/{quote.number}.pdf").status_code == 200
    client.post(f"/portal/quotes/{quote.number}/accept", data={"note": "PO follows"})
    db.expire_all()
    assert db.get(Quote, quote.id).status == "accepted"
    assert db.scalar(select(Task).where(Task.title.contains("accepted")))


def test_other_buyer_cannot_open_my_quote(client, monkeypatch, db, customer):
    use_fake_llm(monkeypatch,
                 tool_msg(("get_price_quote", {"items": [{"product": "DRI", "qty_mt": 20}], "destination": "Raipur"})),
                 text_msg("Quote attached."))
    make_login(db, customer)
    login(client)
    client.post("/portal/chat", json={"message": "20 MT DRI Raipur"})
    number = db.scalar(select(Quote)).number
    assert client.get(f"/portal/quotes/{number}.pdf").status_code == 200
    client.post("/portal/logout")
    client.post("/portal/register", data={"name": "X", "email": "x@example.com", "password": "long-enough"})
    assert client.get(f"/portal/quotes/{number}.pdf").status_code == 404


# ---------------------------------------------------------------- delivery of reminders

def test_payment_reminder_delivered_to_portal_and_email(db, customer, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda to, subject, body, attachment=None: sent.append((to, subject)) or "sent")
    make_login(db, customer, email="accounts@urla.example")
    add_invoice(db, customer, "INV-7", 7, 100000)
    from app.services import receivables

    receivables.run_payment_reminders(db)
    ob = db.scalar(select(Outbox))
    ob.status = "approved"
    assert notify.flush_outbox(db) == {"sent": 1, "failed": 0}
    assert sent == [("accounts@urla.example", "Payment reminder - Shri Hare-Krishna Sponge Iron")]
    assert db.scalar(select(Notification)).customer_id == customer.id and ob.error == ""


def test_delivery_without_login_or_email_is_flagged(db, customer):
    ob = Outbox(customer_id=customer.id, to_name=customer.name, body="hi", kind="reorder", status="approved")
    db.add(ob)
    notify.flush_outbox(db)
    assert ob.status == "sent" and "no portal login" in ob.error and "no email" in ob.error
    stray = Outbox(to_name="Nobody", body="hi", kind="outreach", status="approved")
    db.add(stray)
    notify.flush_outbox(db)
    assert stray.status == "failed"


def test_dispatch_notice_goes_out_immediately(client, db, customer):
    add_orders(db, customer, [0])
    order = db.scalar(select(Order))
    order.status = "confirmed"
    db.commit()
    client.post(f"/staff/orders/{order.id}/dispatch", auth=AUTH,
                data={"truck_no": "cg04ab1", "qty_mt": "10", "notify": "true",
                      "eta": (today() + timedelta(days=1)).isoformat()})
    ob = db.scalar(select(Outbox).where(Outbox.kind == "dispatch"))
    assert ob.status == "approved" and "CG04AB1" in ob.body
