"""
ocr.py -- read scanned PDFs, and judge whether each page was readable.

PURPOSE   Turn an image-only PDF into the same PdfText the text-layer path produces (words with
          page, box in points, and OCR confidence), plus a quality verdict per page.
INPUTS    PDF bytes
OUTPUTS   OcrResult(pdf, quality, images); available() says whether the engine is installed
IMPORTS   pytesseract (needs the tesseract program), pypdfium2, opencv, Pillow
NOTE      Languages: eng plus hin/tam when those tesseract data packs are installed (see languages()).
          The quality gate fails a page when OCR confidence is low, the page is blurry, or
          almost nothing was read. A failed page makes its claims 'Blurry scan'.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytesseract
from pytesseract import Output

from app.documents.pdf_text import Line, PageText, PdfText, Word

os.environ.setdefault("OMP_THREAD_LIMIT", "1")   # one thread per tesseract; we parallelise across pages instead

DPI = 250
OCR_PAGE_TIMEOUT = 60    # seconds per page; a page that takes longer fails like any unreadable scan
MAX_PAGES = 30
CONF_MIN = 65.0          # page mean and per-claim minimum
SHARP_MIN = 150.0        # median Laplacian variance in word boxes: sharp pages ~8000, mild blur 500-1500, unreadable <100
MIN_WORDS = 5


@dataclass(frozen=True)
class PageQuality:
    page: int
    ok: bool
    mean_conf: float
    sharpness: float
    words: int
    reason: str


@dataclass(frozen=True)
class OcrResult:
    pdf: PdfText
    quality: dict
    images: list


@lru_cache(maxsize=1)
def languages() -> str:
    """'eng' plus Hindi and Tamil when their tesseract data is installed (apt install tesseract-ocr-hin tesseract-ocr-tam)."""
    try:
        have = set(pytesseract.get_languages(config=""))
    except Exception:
        return "eng"
    return "+".join(["eng"] + [l for l in ("hin", "tam") if l in have])


def available() -> bool:
    return shutil.which("tesseract") is not None


def render_pages(data: bytes, dpi: int = DPI) -> list:
    doc = pdfium.PdfDocument(data)
    try:
        n = min(len(doc), MAX_PAGES)
        return [doc[i].render(scale=dpi / 72).to_pil().convert("RGB") for i in range(n)]
    finally:
        doc.close()


def _mask_qr(img):
    """White out QR codes before OCR: tesseract invents low-confidence 'words' from them."""
    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    try:
        ok, pts = cv2.QRCodeDetectorAruco().detect(gray)
    except cv2.error:
        return img
    if not ok or pts is None:
        return img
    out = np.array(img)
    for quad in pts.reshape(-1, 4, 2):
        x0, y0 = quad.min(axis=0).astype(int) - 25
        x1, y1 = quad.max(axis=0).astype(int) + 25
        out[max(0, y0):y1, max(0, x0):x1] = 255
    from PIL import Image
    return Image.fromarray(out)


def _sharpness(img, words, k: float) -> float:
    """Mean Laplacian variance inside the word boxes only (a mostly white page would read as 'blurry')."""
    if not words:
        return 0.0
    lap = cv2.Laplacian(np.array(img.convert("L")), cv2.CV_64F)
    vals = []
    for w in words:
        x0, y0, x1, y1 = (int(v / k) for v in (w.x0, w.y0, w.x1, w.y1))
        patch = lap[max(0, y0):y1, max(0, x0):x1]
        if patch.size:
            vals.append(patch.var())
    return float(np.median(vals)) if vals else 0.0


def _page(i: int, img) -> tuple[PageText, PageQuality]:
    full, img = img, _mask_qr(img)
    d = pytesseract.image_to_data(img, lang=languages(), config="--psm 6", output_type=Output.DICT, timeout=OCR_PAGE_TIMEOUT)
    k = 72.0 / DPI
    rows: dict = {}
    for j, raw in enumerate(d["text"]):
        t = raw.strip()
        try:
            conf = float(d["conf"][j])
        except (ValueError, TypeError):
            continue
        if not t or conf < 0:
            continue
        L, T, W, H = d["left"][j], d["top"][j], d["width"][j], d["height"][j]
        rows.setdefault((d["block_num"][j], d["par_num"][j], d["line_num"][j]), []).append(
            Word(t, L * k, T * k, (L + W) * k, (T + H) * k, conf))
    lines = []
    for ws in sorted(rows.values(), key=lambda r: min(w.y0 for w in r)):
        ws = tuple(sorted(ws, key=lambda w: w.x0))
        lines.append(Line(i, " ".join(w.text for w in ws), ws))
    words = [w for ln in lines for w in ln.words]
    mean = sum(w.conf for w in words) / len(words) if words else 0.0
    sharp = _sharpness(img, words, k)
    reason = ""
    if len(words) < MIN_WORDS:
        reason = "almost nothing could be read from this page"
    elif mean < CONF_MIN:
        reason = f"text recognition confidence is low ({mean:.0f}%)"
    elif sharp < SHARP_MIN:
        reason = f"the page is blurry (sharpness {sharp:.0f})"
    q = PageQuality(i, not reason, round(mean, 1), round(sharp, 1), len(words), reason)
    return PageText(i, img.width * k, img.height * k, tuple(lines)), q


def ocr_pdf(data: bytes) -> OcrResult:
    """Raises ValueError if the file cannot be rendered."""
    if not data.startswith(b"%PDF"):
        raise ValueError("not a PDF file")
    try:
        images = render_pages(data)
    except Exception as e:
        raise ValueError(f"could not render PDF: {e}") from e
    if not images:
        raise ValueError("PDF has no pages")
    with ThreadPoolExecutor(max_workers=min(4, len(images))) as ex:
        results = list(ex.map(lambda a: _page(*a), enumerate(images, start=1)))
    pdf = PdfText(hashlib.sha256(data).hexdigest(), tuple(p for p, _ in results))
    return OcrResult(pdf, {q.page: q for _, q in results}, images)
