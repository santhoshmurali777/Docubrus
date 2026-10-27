"""
grounding.py -- no claim without a place on the page (golden rule 1).

PURPOSE   Take proposed values (from pattern matching today, possibly an LLM later) and
          keep only those whose text really appears inside the stated box on the stated
          page of the PDF. Survivors become contracts.Claim; the rest are rejected with
          a reason.
INPUTS    PDF bytes, Proposals, the registry's field types
OUTPUTS   (claims, rejected)
IMPORTS   contracts, correlation.normalizers, ledger.new_id, extract, pdfplumber
"""
from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass

import pdfplumber

from app.contracts import Claim
from app.correlation.normalizers import normalize_date, normalize_number, normalize_text
from app.documents.extract import Proposal
from app.documents.pdf_text import PdfText
from app.ledger import new_id

PAD = 1.5


@dataclass(frozen=True)
class Rejected:
    field_key: str
    raw_value: str
    reason: str


def _squash(s: str) -> str:
    return " ".join(s.split()).casefold()


def normalized_value(field_type: str, raw: str) -> str:
    if field_type == "date":
        d = normalize_date(raw)
        return d.isoformat() if d else _squash(raw)
    if field_type in ("integer", "number"):
        n = normalize_number(raw)
        return f"{n:g}" if n is not None else _squash(raw)
    return normalize_text(raw)


def ground(data: bytes, proposals: list[Proposal], application_id: str, document_id: str,
           field_types: dict[str, str]) -> tuple[list[Claim], list[Rejected]]:
    claims: list[Claim] = []
    rejected: list[Rejected] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for p in proposals:
            if not (1 <= p.page <= len(pdf.pages)):
                rejected.append(Rejected(p.field_key, p.raw_value, f"page {p.page} does not exist"))
                continue
            page = pdf.pages[p.page - 1]
            x0, y0, x1, y1 = p.bbox
            if not (x1 > x0 and y1 > y0):
                rejected.append(Rejected(p.field_key, p.raw_value, "the box is empty"))
                continue
            box = (max(0.0, x0 - PAD), max(0.0, y0 - PAD),
                   min(float(page.width), x1 + PAD), min(float(page.height), y1 + PAD))
            found = page.within_bbox(box).extract_text() or ""
            if _squash(p.raw_value) not in _squash(found):
                rejected.append(Rejected(p.field_key, p.raw_value,
                                         f"value not found inside the box on page {p.page}"))
                continue
            claims.append(Claim(
                id=new_id("clm"), application_id=application_id, document_id=document_id,
                field_key=p.field_key, raw_value=p.raw_value,
                normalized_value=normalized_value(field_types.get(p.field_key, "text"), p.raw_value),
                page=p.page, bbox=(x0, y0, x1, y1), extraction_method="pdf_text" if p.method == "pattern" else p.method))
    return claims, rejected


def ground_ocr(pdf: PdfText, proposals: list[Proposal], application_id: str, document_id: str,
               field_types: dict[str, str]) -> tuple[list[Claim], list[Rejected]]:
    """Same promise as ground(), for scans: the value must be spelled by the OCR words whose
    centres lie inside the stated box on the stated page."""
    claims: list[Claim] = []
    rejected: list[Rejected] = []
    for p in proposals:
        page = next((pg for pg in pdf.pages if pg.page == p.page), None)
        x0, y0, x1, y1 = p.bbox
        if page is None:
            rejected.append(Rejected(p.field_key, p.raw_value, f"page {p.page} does not exist")); continue
        if not (x1 > x0 and y1 > y0):
            rejected.append(Rejected(p.field_key, p.raw_value, "the box is empty")); continue
        inside = [w for ln in page.lines for w in ln.words
                  if x0 - PAD <= (w.x0 + w.x1) / 2 <= x1 + PAD and y0 - PAD <= (w.y0 + w.y1) / 2 <= y1 + PAD]
        # token multiset, not string order: a tilted scan can shuffle words between lines
        want, have = Counter(_squash(p.raw_value).split()), Counter(w.text.casefold() for w in inside)
        if not want or any(have[t] < n for t, n in want.items()):
            rejected.append(Rejected(p.field_key, p.raw_value, f"value not found inside the box on page {p.page}")); continue
        conf = p.conf if p.conf is not None else 0.0
        claims.append(Claim(
            id=new_id("clm"), application_id=application_id, document_id=document_id, field_key=p.field_key,
            raw_value=p.raw_value, normalized_value=normalized_value(field_types.get(p.field_key, "text"), p.raw_value),
            page=p.page, bbox=(x0, y0, x1, y1), extraction_method="ocr", ocr_confidence=round(min(100.0, max(0.0, conf)), 1)))
    return claims, rejected
