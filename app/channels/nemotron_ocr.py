"""NVIDIA Nemotron OCR (NIM) client.

Request (per NVIDIA NIM for Image OCR docs):
    POST <NEMOTRON_OCR_URL>
    {"input": [{"type": "image_url", "url": "data:image/png;base64,..."}], "merge_levels": [...]}
Response:
    {"data": [{"index": 0, "text_detections": [
        {"text_prediction": {"text": "...", "confidence": 0.99},
         "bounding_box": {"points": [{"x": 0.1, "y": 0.2}, ...]}}]}]}

PDFs are rendered to PNG page images first. Detections are regrouped into
reading-order lines (top to bottom, left to right) so the text reads like the document.
"""

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

from app.config import settings

log = logging.getLogger(__name__)

MAX_PAGES = 4
MAX_SIDE = 2400  # px; large phone photos are scaled down before upload
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}


class OCRError(RuntimeError):
    pass


@dataclass
class OCRResult:
    text: str
    confidence: float  # mean confidence of detections, 0-1
    detections: int


def page_images(path: Path) -> list[bytes]:
    """PNG bytes for each page (PDF) or for the single image."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            images = [pdf[i].render(scale=2).to_pil() for i in range(min(len(pdf), MAX_PAGES))]
        finally:
            pdf.close()
    elif suffix in IMAGE_TYPES:
        images = [Image.open(path)]
    else:
        raise OCRError(f"Unsupported file type {suffix}")
    out = []
    for img in images:
        img = img.convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        out.append(buf.getvalue())
    return out


def _points(det: dict) -> list[tuple[float, float]]:
    pts = (det.get("bounding_box") or {}).get("points") or []
    out = []
    for p in pts:
        if isinstance(p, dict):
            out.append((float(p.get("x", 0)), float(p.get("y", 0))))
        elif isinstance(p, (list, tuple)) and len(p) >= 2:
            out.append((float(p[0]), float(p[1])))
    return out


def to_lines(detections: list[dict]) -> list[str]:
    """Group word/phrase boxes into lines in reading order."""
    boxes = []
    for det in detections:
        text = ((det.get("text_prediction") or {}).get("text") or "").strip()
        pts = _points(det)
        if not text:
            continue
        if pts:
            ys = [p[1] for p in pts]
            xs = [p[0] for p in pts]
            boxes.append((sum(ys) / len(ys), max(ys) - min(ys), min(xs), text))
        else:
            boxes.append((float(len(boxes)), 0.0, 0.0, text))
    boxes.sort(key=lambda b: (b[0], b[2]))
    lines: list[list[tuple]] = []
    for box in boxes:
        if lines:
            line = lines[-1]
            line_y = sum(b[0] for b in line) / len(line)
            height = max(max(b[1] for b in line), box[1], 1e-3)
            if abs(box[0] - line_y) < 0.5 * height:
                line.append(box)
                continue
        lines.append([box])
    return [" ".join(b[3] for b in sorted(line, key=lambda b: b[2])) for line in lines]


def ocr_file(path: Path, client: httpx.Client | None = None) -> OCRResult:
    if not settings.ocr_enabled:
        raise OCRError("Nemotron OCR is not configured (NEMOTRON_OCR_URL)")
    pages = page_images(path)
    payload: dict = {"input": [{"type": "image_url", "url": "data:image/png;base64," + base64.b64encode(p).decode()}
                               for p in pages]}
    if settings.ocr_merge_level:
        payload["merge_levels"] = [settings.ocr_merge_level] * len(pages)
    headers = {"Accept": "application/json"}
    if settings.ocr_api_key:
        headers["Authorization"] = f"Bearer {settings.ocr_api_key}"
    http = client or httpx.Client(timeout=90)
    try:
        resp = http.post(settings.ocr_url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise OCRError(f"Could not reach the OCR service: {e}") from e
    finally:
        if client is None:
            http.close()
    if resp.status_code >= 400:
        raise OCRError(f"OCR service error {resp.status_code}: {resp.text[:300]}")

    page_texts, confs = [], []
    for page in sorted(resp.json().get("data", []), key=lambda d: d.get("index", 0)):
        dets = page.get("text_detections") or []
        confs += [float((d.get("text_prediction") or {}).get("confidence", 0)) for d in dets]
        page_texts.append("\n".join(to_lines(dets)))
    text = "\n\n--- next page ---\n\n".join(t for t in page_texts if t)
    return OCRResult(text=text, confidence=round(sum(confs) / len(confs), 3) if confs else 0.0, detections=len(confs))
