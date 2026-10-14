import itertools
import json
from datetime import timedelta

import pytest
from groq.types.chat import ChatCompletion
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Customer, DailyRate, Invoice, Order, Product, SalesRep
from app.seed import BASE_RATES, seed_catalogue
from app.util import today


@pytest.fixture
def db(tmp_path):
    from app.config import settings

    original_dir = settings.data_dir
    object.__setattr__(settings, "data_dir", tmp_path)  # Settings is frozen; files go to tmp
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    import app.models  # noqa: F401

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    seed_catalogue(session)
    for p in session.scalars(select(Product).where(Product.status == "live")):
        session.add(DailyRate(product_id=p.id, rate_date=today(), base_rate_per_mt=BASE_RATES[p.code]))
    session.flush()
    yield session
    session.close()
    object.__setattr__(settings, "data_dir", original_dir)


@pytest.fixture
def rep(db):
    r = SalesRep(name="Amit", phone="919800000001", states="Chhattisgarh")
    r2 = SalesRep(name="Sneha", phone="919800000002", states="Maharashtra")
    db.add_all([r, r2])
    db.flush()
    return r


@pytest.fixture
def customer(db, rep):
    c = Customer(name="Urla Ispat", phone="919900000102", city="Raipur", state="Chhattisgarh", credit_days=15, rep_id=rep.id)
    db.add(c)
    db.flush()
    return c


def add_orders(db, customer, gaps_days_ago: list[int], qty=100.0, product_code="DRI"):
    product = db.scalar(select(Product).where(Product.code == product_code))
    for i, ago in enumerate(gaps_days_ago):
        o = Order(number=f"SO-{customer.id}-{i}", customer_id=customer.id, order_date=today() - timedelta(days=ago),
                  product_id=product.id, item_text=product.name, qty_mt=qty, rate_per_mt=27000, amount=qty * 27000,
                  status="delivered")
        db.add(o)
    db.flush()


def add_invoice(db, customer, number, days_overdue, amount, paid=0.0):
    inv = Invoice(number=number, customer_id=customer.id, invoice_date=today() - timedelta(days=days_overdue + 15),
                  due_date=today() - timedelta(days=days_overdue), amount=amount, paid_amount=paid)
    db.add(inv)
    db.flush()
    return inv


# ---------------------------------------------------------------- fake Groq

_ids = itertools.count(1)


def text_msg(text: str) -> ChatCompletion:
    return _completion({"role": "assistant", "content": text}, "stop")


def tool_msg(*calls: tuple[str, dict], text: str = "") -> ChatCompletion:
    tool_calls = [
        {"id": f"call_{next(_ids)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        for name, args in calls
    ]
    return _completion({"role": "assistant", "content": text or None, "tool_calls": tool_calls}, "tool_calls")


def _completion(message: dict, finish: str) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": f"chatcmpl_{next(_ids)}", "object": "chat.completion", "created": 0, "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    })


class FakeClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        outer = self

        class _Completions:
            def create(self, **kwargs):
                import copy

                outer.requests.append(copy.deepcopy(kwargs))
                return outer.responses.pop(0)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()
