"""Customer portal accounts.

Two ways in:
  * Staff invite: from a customer's page staff create a login linked to that
    Customer and share the one-time activation link. Only invited logins can
    see orders, invoices and certificates.
  * Self sign-up: any buyer can register; they get a Lead (not a Customer) and
    see only their own chats and quotes until staff link them to a customer.
"""

import hashlib
import hmac
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Customer, PortalUser
from app.services import crm
from app.util import log_activity, norm_phone, now

MIN_PASSWORD = 8


class PortalError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def by_email(db: Session, email: str) -> PortalUser | None:
    return db.scalar(select(PortalUser).where(func.lower(PortalUser.email) == _norm_email(email)))


def register(db: Session, name: str, company: str, email: str, phone: str, password: str,
             city: str = "", state: str = "") -> PortalUser:
    email = _norm_email(email)
    if "@" not in email:
        raise PortalError("Please enter a valid email address.")
    if len(password) < MIN_PASSWORD:
        raise PortalError(f"Password must be at least {MIN_PASSWORD} characters.")
    if by_email(db, email):
        raise PortalError("An account with this email already exists. Please log in.")
    # Always a fresh lead: matching by phone would let someone typing another buyer's number see
    # that buyer's quotes. Staff link the login to a customer account after checking.
    lead = crm.upsert_lead(db, "", "portal", {"name": name, "company": company, "email": email,
                                              "city": city, "state": state})
    lead.phone = norm_phone(phone)
    user = PortalUser(name=name.strip(), company=company.strip(), email=email, phone=lead.phone,
                      password_hash=hash_password(password), lead_id=lead.id)
    db.add(user)
    db.flush()
    log_activity(db, "lead", lead.id, "portal", f"Registered on portal as {email}")
    return user


def authenticate(db: Session, email: str, password: str) -> PortalUser | None:
    user = by_email(db, email)
    if user is None or not user.active or not user.password_hash or not check_password(password, user.password_hash):
        return None
    user.last_login = now()
    return user


def invite(db: Session, customer: Customer, email: str, name: str = "") -> PortalUser:
    """Create or re-issue a login for a customer. Returns the user; share /portal/activate/<invite_token>."""
    email = _norm_email(email)
    if "@" not in email:
        raise PortalError("Enter the customer's email address.")
    user = by_email(db, email)
    if user and user.customer_id not in (None, customer.id):
        raise PortalError("That email already belongs to another customer's login.")
    if user is None:
        user = PortalUser(email=email, name=name or customer.name, company=customer.name, phone=customer.phone)
        db.add(user)
    user.customer_id = customer.id
    user.active = True
    user.invite_token = secrets.token_urlsafe(24)
    if not customer.email:
        customer.email = email
    db.flush()
    return user


def activate(db: Session, token: str, password: str) -> PortalUser:
    user = db.scalar(select(PortalUser).where(PortalUser.invite_token == token))
    if user is None:
        raise PortalError("This activation link is invalid or already used.")
    if len(password) < MIN_PASSWORD:
        raise PortalError(f"Password must be at least {MIN_PASSWORD} characters.")
    user.password_hash = hash_password(password)
    user.invite_token = None
    user.active = True
    db.flush()
    return user
