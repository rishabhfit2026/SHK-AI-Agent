"""Small helpers shared across modules. Times are naive IST (the plant runs on IST)."""

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None, microsecond=0)


def today() -> date:
    return now().date()


def norm_phone(raw: str | None) -> str:
    """Digits only, with India's 91 prefix added to bare 10-digit mobiles."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = "91" + digits
    return digits


def inr(amount: float | None) -> str:
    """Indian digit grouping: 1234567.5 -> '₹12,34,567.50'."""
    if amount is None:
        return "-"
    neg = amount < 0
    whole, frac = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def next_number(db, name: str, prefix: str) -> str:
    from app.models import Counter

    counter = db.get(Counter, name)
    if counter is None:
        counter = Counter(name=name, value=0)
        db.add(counter)
    counter.value += 1
    db.flush()
    return f"{prefix}-{today().year}-{counter.value:04d}"


def log_activity(db, entity: str, entity_id: int, kind: str, text: str, actor: str = "system") -> None:
    from app.models import Activity

    db.add(Activity(entity=entity, entity_id=entity_id, kind=kind, text=text, actor=actor))
