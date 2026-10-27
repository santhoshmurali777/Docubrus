"""
perturb.py -- degrade the clean synthetic PDFs the way real-world scanning does.

PURPOSE   Turn a text PDF into an image-only scan (page 1 rasterised, printed QR link replaced by a real
          QR image, as in tests/scan_helpers.py), then damage it at one of 5 severity levels.
INPUTS    pdf bytes, perturbation name, severity 1..5
OUTPUTS   pdf bytes (image-only, so the pipeline must OCR it)
"""
from __future__ import annotations

import io
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.documents.extract import extract_qr_url
from app.documents.ocr import DPI, render_pages
from app.documents.pdf_text import read_pdf

SEVERITIES = (1, 2, 3, 4, 5)

# severity 1..5 for each perturbation
PARAMS = {
    "none":        (0, 0, 0, 0, 0),
    "rotate":      (0.5, 1.0, 1.5, 2.0, 3.0),          # degrees
    "blur":        (0.5, 0.8, 1.2, 1.8, 2.5),          # gaussian sigma, px at 250 dpi
    "jpeg":        (70, 60, 50, 40, 30),               # quality
    "noise":       (4, 8, 12, 18, 25),                 # gaussian sigma, grey levels
    "low_dpi":     (200, 170, 140, 110, 90),           # effective dpi (down- then up-sampled)
    "perspective": (0.005, 0.01, 0.02, 0.03, 0.045),   # corner shift as fraction of page size (phone photo)
    "stamp":       (0.10, 0.14, 0.18, 0.22, 0.27),     # stamp diameter as fraction of page width
}
NAMES = [n for n in PARAMS if n != "none"]


def _qr(url: str, px: int) -> Image.Image:
    q = cv2.QRCodeEncoder.create().encode(url)
    q = cv2.resize(q, (px, px), interpolation=cv2.INTER_NEAREST)
    q = cv2.copyMakeBorder(q, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
    return Image.fromarray(q).convert("RGB")


def base_scan(pdf_bytes: bytes) -> Image.Image:
    """Page 1 as an image, with a real QR image in place of the printed link."""
    url = extract_qr_url(read_pdf(pdf_bytes))
    page = render_pages(pdf_bytes)[0]
    w, h = page.size
    if url:
        top = int(h * 0.80)
        page.paste((255, 255, 255), (0, top, w, h))
        page.paste(_qr(url, int(h * 0.16)), (int(w * 0.07), top + 10))
    return page


def _rotate(im, v, rng):
    return im.rotate(v * rng.choice([-1, 1]), fillcolor=(255, 255, 255), resample=Image.BICUBIC)


def _blur(im, v, rng):
    return im.filter(ImageFilter.GaussianBlur(v))


def _jpeg(im, v, rng):
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=int(v)); buf.seek(0)
    return Image.open(buf).convert("RGB")


def _noise(im, v, rng):
    a = np.array(im).astype(np.int16) + rng.normal(0, v, (im.height, im.width, 1)).astype(np.int16)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def _low_dpi(im, v, rng):
    s = v / DPI
    small = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.BILINEAR)
    return small.resize(im.size, Image.BILINEAR)


def _perspective(im, v, rng):
    w, h = im.size
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    d = rng.uniform(-1, 1, (4, 2)) * v * np.array([w, h])
    d = np.where(np.abs(d) < v * 0.5 * np.array([w, h]), np.sign(d) * v * 0.5 * np.array([w, h]), d)  # no near-zero shifts
    m = cv2.getPerspectiveTransform(src, np.float32(src + d))
    out = cv2.warpPerspective(np.array(im), m, (w, h), borderValue=(255, 255, 255))
    return Image.fromarray(out)


def _stamp(im, v, rng):
    """A semi-transparent round rubber stamp over the body text (never over the QR)."""
    w, h = im.size
    dia = int(w * v)
    cx, cy = int(w * 0.72), int(h * 0.45)
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    col = (30, 50, 170, 150)
    d.ellipse((cx - dia // 2, cy - dia // 2, cx + dia // 2, cy + dia // 2), outline=col, width=max(4, dia // 25))
    d.ellipse((cx - dia // 2 + dia // 8,) * 2 + (cx + dia // 2 - dia // 8,) * 2, outline=col, width=max(2, dia // 50))
    try:
        font = ImageFont.load_default(size=max(14, dia // 6))
    except TypeError:
        font = ImageFont.load_default()
    d.text((cx, cy), "RECEIVED", fill=col, font=font, anchor="mm")
    return Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")


_FN = {"rotate": _rotate, "blur": _blur, "jpeg": _jpeg, "noise": _noise,
       "low_dpi": _low_dpi, "perspective": _perspective, "stamp": _stamp}


def degrade(pdf_bytes: bytes, name: str, severity: int, seed: int = 1) -> bytes:
    if name not in PARAMS:
        raise ValueError(f"unknown perturbation '{name}'")
    if severity not in SEVERITIES:
        raise ValueError("severity must be 1..5")
    im = base_scan(pdf_bytes)
    if name != "none":
        im = _FN[name](im, PARAMS[name][severity - 1], np.random.default_rng(seed))
    buf = io.BytesIO()
    im.save(buf, format="PDF", resolution=DPI)
    return buf.getvalue()
