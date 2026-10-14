import io
import json
from datetime import timedelta

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app.channels import nemotron_ocr
from app.channels.nemotron_ocr import OCRResult
from app.config import settings
from app.models import Customer, DocumentScan, Invoice, Notification, Outbox, Payment, Task
from app.services import documents, notify
from app.util import today
from tests.conftest import FakeClient, add_invoice, text_msg
from tests.test_app import AUTH, client, make_login, login  # noqa: F401  (fixtures/helpers)


def png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 30), "white").save(buf, format="PNG")
    return buf.getvalue()


def det(text, x, y, w=0.1, h=0.02, conf=0.95):
    return {"text_prediction": {"text": text, "confidence": conf},
            "bounding_box": {"points": [{"x": x, "y": y}, {"x": x + w, "y": y}, {"x": x + w, "y": y + h},
                                        {"x": x, "y": y + h}]}}


@pytest.fixture
def ocr_on():
    object.__setattr__(settings, "ocr_url", "http://ocr.test/v1/infer")
    object.__setattr__(settings, "ocr_api_key", "nvapi-test")
    yield
    object.__setattr__(settings, "ocr_url", "")
    object.__setattr__(settings, "ocr_api_key", "")


# ---------------------------------------------------------------- OCR client

def test_detections_are_rebuilt_into_reading_order_lines():
    dets = [det("₹5,00,000", 0.6, 0.302), det("Amount:", 0.1, 0.30), det("UTR:", 0.1, 0.40),
            det("SBIN0123456789", 0.3, 0.401), det("NEFT", 0.1, 0.10)]
    assert nemotron_ocr.to_lines(dets) == ["NEFT", "Amount: ₹5,00,000", "UTR: SBIN0123456789"]
    # Older NIM builds return points as [x, y] pairs.
    pairs = [{"text_prediction": {"text": "B"}, "bounding_box": {"points": [[0.5, 0.1], [0.6, 0.1], [0.6, 0.12], [0.5, 0.12]]}},
             {"text_prediction": {"text": "A"}, "bounding_box": {"points": [[0.1, 0.1], [0.2, 0.1], [0.2, 0.12], [0.1, 0.12]]}}]
    assert nemotron_ocr.to_lines(pairs) == ["A B"]


def test_ocr_request_format_and_pdf_pages(tmp_path, ocr_on):
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "slip.pdf"
    c = canvas.Canvas(str(pdf))
    c.drawString(100, 750, "page one")
    c.showPage()
    c.drawString(100, 750, "page two")
    c.save()
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [
            {"index": 1, "text_detections": [det("second", 0.1, 0.1, conf=0.8)]},
            {"index": 0, "text_detections": [det("first", 0.1, 0.1, conf=1.0)]},
        ]})

    result = nemotron_ocr.ocr_file(pdf, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["auth"] == "Bearer nvapi-test"
    assert len(seen["body"]["input"]) == 2
    assert seen["body"]["input"][0]["type"] == "image_url"
    assert seen["body"]["input"][0]["url"].startswith("data:image/png;base64,")
    assert result.text.startswith("first") and result.text.endswith("second")
    assert result.confidence == 0.9 and result.detections == 2


def test_ocr_errors_are_reported(tmp_path, ocr_on):
    img = tmp_path / "x.png"
    img.write_bytes(png_bytes())
    failing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="busy")))
    with pytest.raises(nemotron_ocr.OCRError, match="503"):
        nemotron_ocr.ocr_file(img, client=failing)


# ---------------------------------------------------------------- processing + confirmation

def fake_ocr(text, confidence=0.97):
    return lambda path: OCRResult(text=text, confidence=confidence, detections=10)


def new_scan(db, kind="payment", customer=None, source="staff"):
    return documents.save_upload(db, kind=kind, filename="slip.png", content=png_bytes(), source=source,
                                 uploaded_by="tester", customer_id=customer.id if customer else None)


def test_payment_proof_is_read_matched_and_recorded(db, customer):
    inv = add_invoice(db, customer, "INV-55", 3, 500000)
    scan = new_scan(db, customer=customer, source="portal")
    llm_out = text_msg(json.dumps({"amount": 500000, "paid_on": str(today()), "reference": "SBIN026100123456",
                                   "mode": "RTGS", "payer_name": "Urla Ispat Pvt Ltd", "payer_bank": "SBI",
                                   "payee_name": "SHRI HARE KRISHNA SPONGE IRON LTD", "invoice_numbers": []}))
    documents.process_scan(db, scan, ocr=fake_ocr("RTGS ... 5,00,000 ..."), client=FakeClient(llm_out))
    assert scan.status == "needs_review" and scan.extracted["amount"] == 500000
    assert scan.extracted["suggested_invoice_id"] == inv.id  # matched by outstanding amount
    assert scan.warnings == []

    documents.confirm_payment(db, scan, customer_id=customer.id, amount=500000, paid_on=today(),
                              reference="SBIN026100123456", mode="RTGS", invoice_id=inv.id, user="accounts")
    assert inv.outstanding == 0 and scan.status == "confirmed" and scan.payment_id
    thanks = db.scalar(select(Outbox).where(Outbox.kind == "payment_received"))
    assert thanks.status == "approved" and "5,00,000" in thanks.body

    # Same UTR again is flagged during review and refused on confirm.
    again = new_scan(db, customer=customer)
    documents.process_scan(db, again, ocr=fake_ocr("dup"), client=FakeClient(llm_out))
    assert any("already recorded" in w for w in again.warnings)
    with pytest.raises(ValueError, match="already recorded"):
        documents.confirm_payment(db, again, customer_id=customer.id, amount=500000, paid_on=today(),
                                  reference="SBIN026100123456", mode="RTGS", invoice_id=None, user="accounts")


def test_payment_checks_catch_wrong_beneficiary_and_unknown_customer(db, customer):
    scan = new_scan(db)
    llm_out = text_msg(json.dumps({"amount": 1000, "paid_on": str(today() + timedelta(days=3)), "reference": "X1",
                                   "payee_name": "Some Other Traders", "payer_name": "Unknown Person"}))
    documents.process_scan(db, scan, ocr=fake_ocr("...", confidence=0.6), client=FakeClient(llm_out))
    joined = " ".join(scan.warnings)
    for expected in ("not SHK", "future", "Customer not identified", "Low OCR confidence"):
        assert expected in joined


def test_payment_customer_matched_from_payer_name(db, customer):
    scan = new_scan(db)
    llm_out = text_msg(json.dumps({"amount": 1000, "reference": "R1", "payer_name": "URLA ISPAT PVT. LTD.",
                                   "payee_name": "Shri Hare Krishna Sponge Iron"}))
    documents.process_scan(db, scan, ocr=fake_ocr("..."), client=FakeClient(llm_out))
    assert scan.customer_id == customer.id and any("please verify" in w for w in scan.warnings)


def test_cannot_apply_payment_to_another_customers_invoice(db, customer):
    other = Customer(name="Other Co")
    db.add(other)
    db.flush()
    theirs = add_invoice(db, other, "INV-OTHER", 2, 1000)
    scan = new_scan(db, customer=customer)
    scan.status = "needs_review"
    with pytest.raises(ValueError, match="doesn't belong"):
        documents.confirm_payment(db, scan, customer_id=customer.id, amount=1000, paid_on=today(), reference="R9",
                                  mode="", invoice_id=theirs.id, user="a")


def test_invoice_scan_creates_customer_and_invoice(db):
    scan = new_scan(db, kind="invoice")
    llm_out = text_msg(json.dumps({
        "invoice_number": "SHK/26-27/0456", "invoice_date": "2026-10-01", "due_date": None,
        "buyer_name": "Nagpur Re-Rollers", "buyer_gstin": "27BBBBB0000B1Z5", "buyer_city": "Nagpur",
        "buyer_state": "Maharashtra", "item": "MS Billets 100x100", "qty_mt": 30, "rate_per_mt": 40500,
        "taxable_value": 1215000, "gst_amount": 218700, "total_amount": 1433700}))
    documents.process_scan(db, scan, ocr=fake_ocr("TAX INVOICE ..."), client=FakeClient(llm_out))
    assert scan.customer_id is None and any("new customer" in w for w in scan.warnings)
    assert not any("doesn't match" in w for w in scan.warnings)

    f = scan.extracted
    inv = documents.confirm_invoice(
        db, scan, customer_id=None, new_customer_name=f["buyer_name"], gstin=f["buyer_gstin"], city="Nagpur",
        state="Maharashtra", number=f["invoice_number"], invoice_date=today(), due_date=None,
        amount=f["total_amount"], item=f["item"], qty_mt=30, rate_per_mt=40500, user="accounts")
    assert inv.customer.gstin == "27BBBBB0000B1Z5" and inv.outstanding == 1433700
    assert db.scalar(select(Invoice).where(Invoice.number == "SHK/26-27/0456"))

    dup = new_scan(db, kind="invoice")
    documents.process_scan(db, dup, ocr=fake_ocr("..."), client=FakeClient(llm_out))
    assert dup.customer_id == inv.customer_id  # matched by GSTIN now
    assert any("already exists" in w for w in dup.warnings)


def test_ocr_not_configured_still_allows_manual_entry(db, customer):
    scan = new_scan(db, customer=customer)
    documents.process_scan(db, scan)
    assert scan.status == "needs_review" and "not configured" in scan.error


def test_upload_validation(db):
    with pytest.raises(ValueError, match="PDF or a photo"):
        documents.save_upload(db, kind="payment", filename="x.exe", content=b"MZ", source="staff", uploaded_by="t")
    with pytest.raises(ValueError, match="10 MB"):
        documents.save_upload(db, kind="payment", filename="x.png", content=b"0" * (11 * 1024 * 1024),
                              source="staff", uploaded_by="t")


# ---------------------------------------------------------------- web flows

def test_customer_uploads_proof_and_staff_confirms(client, db, customer):  # noqa: F811
    add_invoice(db, customer, "INV-9", 5, 2000)
    make_login(db, customer)
    login(client)
    r = client.post("/portal/payments/upload", files={"file": ("neft.png", png_bytes(), "image/png")},
                    data={"note": "against INV-9"}, follow_redirects=False)
    assert r.headers["location"] == "/portal/account?uploaded=1"
    db.expire_all()
    scan = db.scalar(select(DocumentScan))
    assert scan.source == "portal" and scan.customer_id == customer.id and scan.status == "needs_review"
    assert db.scalar(select(Task).where(Task.title.startswith("Verify payment proof")))
    assert "being verified" in client.get("/portal/account").text

    assert client.get("/staff/documents", auth=AUTH).status_code == 200
    page = client.get(f"/staff/documents/{scan.id}", auth=AUTH).text
    assert "Record payment" in page and "against INV-9" in page
    assert client.get(f"/staff/documents/{scan.id}/file", auth=AUTH).status_code == 200
    r = client.post(f"/staff/documents/{scan.id}/confirm-payment", auth=AUTH, follow_redirects=False,
                    data={"customer_id": customer.id, "amount": "2000", "paid_on": str(today()),
                          "reference": "UTR777", "mode": "NEFT", "invoice_id": "0"})
    assert r.status_code == 303
    db.expire_all()
    assert db.scalar(select(Payment)).ref == "UTR777"
    notify.flush_outbox(db)
    assert db.scalar(select(Notification).where(Notification.kind == "payment_received"))
    assert "payment recorded" in client.get("/portal/account").text


def test_portal_rejects_bad_files(client, db, customer):  # noqa: F811
    make_login(db, customer)
    login(client)
    r = client.post("/portal/payments/upload", files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
                    follow_redirects=False)
    assert "error=" in r.headers["location"]
    assert db.scalar(select(DocumentScan)) is None


def test_quantity_read_1000x_too_big_is_corrected(db):
    fields = {"invoice_number": "X1", "invoice_date": "2026-10-01", "qty_mt": 30000, "rate_per_mt": 40500,
              "taxable_value": 1215000, "gst_amount": 218700, "total_amount": 1433700}
    warns = documents.check_invoice(db, fields)
    assert fields["qty_mt"] == 30 and any("corrected" in w for w in warns)
    fields = {"invoice_number": "X2", "invoice_date": "2026-10-01", "qty_mt": 25, "rate_per_mt": 40500,
              "taxable_value": 1215000, "total_amount": 1433700}
    assert any("but taxable value" in w for w in documents.check_invoice(db, fields))
