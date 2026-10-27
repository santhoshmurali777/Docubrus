"""
extract.py -- find the registry's fields in a PDF's text and propose claims.

PURPOSE   For a document type, look for lines of the form "<label>: <value>" using
          the labels in config/document_registry.json. Each hit becomes a Proposal
          carrying page and bounding box of the VALUE.
INPUTS    PdfText, parsed document_registry.json
OUTPUTS   detect_doc_type(), extract_fields() -> list[Proposal], extract_qr_url()
IMPORTS   pdf_text
NOTE      Proposals are not claims yet. documents/grounding.py must confirm each one
          against the page before it becomes a Claim.
          The QR payload is read from printed text ("Verification link:" block) because
          no QR image decoder is part of this build.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from dataclasses import dataclass
from typing import Optional

from app.documents.pdf_text import Line, PdfText

QR_MARKER = "verification link"
_URL_CHUNK = re.compile(r"^[A-Za-z0-9_\-=&?:/.%+~]+$")


@dataclass(frozen=True)
class Proposal:
    field_key: str
    raw_value: str
    page: int
    bbox: tuple[float, float, float, float]
    method: str = "pattern"
    conf: Optional[float] = None         # mean OCR confidence of the value words (None = text layer)


FUZZY_MIN = 0.86


def _best_ratio(label: str, head: str) -> float:
    n = len(label)
    if label in head:
        return 1.0
    return max((SequenceMatcher(None, label, head[i:i + n]).ratio() for i in range(max(1, len(head) - n + 1))), default=0.0)


def detect_doc_type(pdf: PdfText, registry: dict, fuzzy: bool = False) -> Optional[str]:
    """Match a registry label against the top of page 1. Longest label wins.
    fuzzy=True tolerates OCR slips in the heading (still needs a close match)."""
    if not pdf.pages:
        return None
    head = " ".join(ln.text for ln in pdf.pages[0].lines[:6]).casefold()
    if not fuzzy:
        hits = [(len(spec["label"]), dt) for dt, spec in registry["document_types"].items()
                if spec["label"].casefold() in head]
        return max(hits)[1] if hits else None
    scored = [(_best_ratio(spec["label"].casefold(), head), len(spec["label"]), dt)
              for dt, spec in registry["document_types"].items()]
    best = max(scored)
    return best[2] if best[0] >= FUZZY_MIN else None


def _label_table(spec: dict) -> dict[str, str]:
    table: dict[str, str] = {}
    for key, f in spec["fields"].items():
        for lab in [f["label"], *f.get("labels", [])]:
            table.setdefault(lab.casefold(), key)
    return table


def _value_part(line: Line) -> Optional[tuple[str, str, tuple[float, float, float, float], Optional[float]]]:
    """Split a line at its first colon -> (label, value, value bbox)."""
    for i, w in enumerate(line.words):
        if ":" in w.text:
            before, _, after = w.text.partition(":")
            label = " ".join([*(x.text for x in line.words[:i]), before]).strip()
            rest = line.words[i + 1:]
            parts = ([after] if after else []) + [x.text for x in rest]
            value = " ".join(parts).strip()
            if not value:
                return None
            boxed = ([w] if after else []) + list(rest)
            bbox = (min(x.x0 for x in boxed), min(x.y0 for x in boxed),
                    max(x.x1 for x in boxed), max(x.y1 for x in boxed))
            confs = [x.conf for x in boxed if x.conf is not None]
            return label, value, bbox, (sum(confs) / len(confs) if confs else None)
    return None


def extract_fields(pdf: PdfText, doc_type: str, registry: dict) -> list[Proposal]:
    spec = registry["document_types"][doc_type]
    table = _label_table(spec)
    seen: set[str] = set()
    out: list[Proposal] = []
    for ln in pdf.lines:
        part = _value_part(ln)
        if not part:
            continue
        label, value, bbox, conf = part
        key = table.get(label.casefold())
        if key and key not in seen:          # first occurrence wins
            seen.add(key)
            out.append(Proposal(key, value, ln.page, bbox, "ocr" if conf is not None else "pattern", conf))
    return out


def extract_qr_url(pdf: PdfText) -> Optional[str]:
    """Rejoin the printed verification link, which is wrapped over several lines."""
    lines = pdf.lines
    for i, ln in enumerate(lines):
        if QR_MARKER in ln.text.casefold():
            chunks = []
            for nxt in lines[i + 1:]:
                if nxt.page == ln.page and _URL_CHUNK.match(nxt.text):
                    chunks.append(nxt.text)
                else:
                    break
            return "".join(chunks) or None
    return None
