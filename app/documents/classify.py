"""
classify.py -- work out WHAT a PDF is, and what kind of proof it carries.

PURPOSE   Today detect_doc_type() needs one registry label in the first 6 lines. Real documents are
          messier. This scores every known document type from heading phrases and body keywords,
          returns ranked candidates with the matching text, and says "unknown" rather than guess.
          It also profiles the file: text layer or scan, digital signature present, QR link and
          whether that link is signed or a plain lookup link.
INPUTS    PDF bytes, config/document_registry.json, config/doc_signatures.json
OUTPUTS   Classification (JSON-ready via .as_dict())
IMPORTS   pdf_text, extract (extract_qr_url), qr (optional, for QR images)
NOTE      Transparent scoring, not a trained model: there is no labelled data yet. Every score can be
          traced to phrases found on a page. A reviewer's correction is the future training signal.
          Classifying a document does NOT authenticate it. It only picks which checks to run.
"""
from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.documents.extract import extract_qr_url
from app.documents.pdf_text import PdfText, read_pdf

CONFIG = os.path.join(os.path.dirname(__file__), "..", "..", "config")
TITLE_W, HEAD_W, KEY_W, KEY_CAP = 3.0, 1.5, 0.5, 2.0
CONFIDENT_MIN, CONFIDENT_MARGIN, POSSIBLE_MIN = 3.0, 1.5, 1.5
HEAD_LINES = 8


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.casefold()).strip()


def load_signatures(registry: Optional[dict] = None, path: Optional[str] = None) -> dict:
    """Known types: registry types (label = title) merged with config/doc_signatures.json."""
    if registry is None:
        with open(os.path.join(CONFIG, "document_registry.json"), encoding="utf-8") as fh:
            registry = json.load(fh)
    with open(path or os.path.join(CONFIG, "doc_signatures.json"), encoding="utf-8") as fh:
        extra = json.load(fh)["types"]
    out: dict[str, dict] = {}
    for dt, spec in registry["document_types"].items():
        fields = [f["label"] for f in spec["fields"].values()]
        out[dt] = {"label": spec["label"], "titles": [spec["label"]], "keywords": fields,
                   "route": spec["proof"]["kind"], "in_registry": True}
    for dt, spec in extra.items():
        base = out.get(dt, {"titles": [], "keywords": [], "in_registry": False})
        out[dt] = {"label": spec["label"], "titles": base["titles"] + spec["titles"],
                   "keywords": base["keywords"] + spec["keywords"], "route": spec["route"],
                   "in_registry": base["in_registry"]}
    return out


@dataclass(frozen=True)
class Candidate:
    doc_type: str
    label: str
    score: float
    matched: tuple[str, ...]


@dataclass(frozen=True)
class Classification:
    verdict: str                     # confident | possible | unknown | unreadable
    doc_type: Optional[str]
    label: Optional[str]
    route: Optional[str]
    in_registry: bool
    candidates: tuple[Candidate, ...]
    nature: dict
    reason: str

    def as_dict(self) -> dict:
        return {"verdict": self.verdict, "doc_type": self.doc_type, "label": self.label, "route": self.route,
                "in_registry": self.in_registry, "reason": self.reason, "nature": self.nature,
                "candidates": [{"doc_type": c.doc_type, "label": c.label, "score": round(c.score, 2),
                                "matched": list(c.matched)} for c in self.candidates]}


def _score(pdf: PdfText, spec: dict) -> tuple[float, list[str]]:
    first = pdf.pages[0]
    head = _norm(" ".join(ln.text for ln in first.lines[:HEAD_LINES]))
    page1 = _norm(" ".join(ln.text for ln in first.lines))
    body = _norm(" ".join(ln.text for ln in pdf.lines))
    score, matched = 0.0, []
    for t in spec["titles"]:
        nt = _norm(t)
        if nt in head:
            score += TITLE_W; matched.append(f"heading: {t}")
        elif nt in page1:
            score += HEAD_W; matched.append(f"page 1: {t}")
    kw = [k for k in spec["keywords"] if _norm(k) in body]
    score += min(KEY_CAP, KEY_W * len(kw))
    matched += [f"text: {k}" for k in kw[:6]]
    return score, matched


def _qr_kind(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    q = parse_qs(urlparse(url).query)
    return "signed_payload" if ("d" in q and "s" in q) else "plain_lookup_link"


def _qr_from_images(data: bytes) -> Optional[str]:
    """Best effort: render page 1 and decode a QR image. Silent on any failure (optional dependency)."""
    try:
        import pdfplumber
        from app.documents.qr import decode_images
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return decode_images([pdf.pages[0].to_image(resolution=150).original])
    except Exception:
        return None


def profile(data: bytes, pdf: PdfText) -> dict:
    url = extract_qr_url(pdf) or _qr_from_images(data)
    host = urlparse(url).hostname if url else None
    return {"pages": len(pdf.pages), "text_layer": pdf.has_text_layer,
            "digital_signature": b"/ByteRange" in data and b"/Contents" in data,
            "qr_link": url, "qr_host": host, "qr_kind": _qr_kind(url)}


def classify_pdf(data: bytes, signatures: Optional[dict] = None, registry: Optional[dict] = None) -> Classification:
    sigs = signatures or load_signatures(registry)
    try:
        pdf = read_pdf(data)
    except ValueError as e:
        return Classification("unreadable", None, None, None, False, (), {}, str(e))
    nature = profile(data, pdf)
    if not pdf.has_text_layer:
        return Classification("unreadable", None, None, None, False, (), nature,
                              "no text layer (a scan): needs OCR before the type can be read; not guessed")
    cands = []
    for dt, spec in sigs.items():
        s, m = _score(pdf, spec)
        if s > 0:
            cands.append(Candidate(dt, spec["label"], s, tuple(m)))
    cands.sort(key=lambda c: -c.score)
    top = tuple(cands[:3])
    if not top or top[0].score < POSSIBLE_MIN:
        return Classification("unknown", None, None, None, False, top, nature,
                              "no known document type matched well enough; send to a human")
    best = top[0]
    margin = best.score - (top[1].score if len(top) > 1 else 0.0)
    spec = sigs[best.doc_type]
    if best.score >= CONFIDENT_MIN and margin >= CONFIDENT_MARGIN:
        verdict, why = "confident", f"'{best.label}' scored {best.score:.1f}, {margin:.1f} ahead of the next type"
    else:
        verdict, why = "possible", f"closest is '{best.label}' ({best.score:.1f}, {margin:.1f} ahead); a reviewer should confirm"
    return Classification(verdict, best.doc_type, best.label, spec["route"], spec["in_registry"], top, nature, why)
