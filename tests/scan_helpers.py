"""Turn a synthetic text PDF into an image-only scan with a real QR image (for testing the OCR path)."""
import io
import cv2
import numpy as np
from PIL import Image, ImageFilter
from app.documents.extract import extract_qr_url
from app.documents.ocr import render_pages
from app.documents.pdf_text import read_pdf


def qr_image(url: str, px: int = 600) -> Image.Image:
    q = cv2.QRCodeEncoder.create().encode(url)
    q = cv2.resize(q, (px, px), interpolation=cv2.INTER_NEAREST)
    q = cv2.copyMakeBorder(q, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255)
    return Image.fromarray(q).convert("RGB")


def make_scan(pdf_bytes: bytes, blur: float = 0.0, noise: float = 0.0, tilt: float = 0.0, seed: int = 1) -> bytes:
    url = extract_qr_url(read_pdf(pdf_bytes))
    page = render_pages(pdf_bytes)[0]
    w, h = page.size
    if url:                                          # wipe the printed link, paste a real QR image
        top = int(h * 0.80)
        page.paste((255, 255, 255), (0, top, w, h))
        q = qr_image(url, px=int(h * 0.16))
        page.paste(q, (int(w * 0.07), top + 10))
    if tilt:
        page = page.rotate(tilt, expand=False, fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    if blur:
        page = page.filter(ImageFilter.GaussianBlur(blur))
    if noise:
        a = np.array(page).astype(np.int16)
        a += np.random.default_rng(seed).normal(0, noise, a.shape).astype(np.int16)
        page = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    page.save(buf, format="PDF", resolution=250)
    return buf.getvalue()
