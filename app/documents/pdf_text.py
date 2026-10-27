"""
pdf_text.py -- read the text layer of a PDF, with the position of every line.

PURPOSE   Turn PDF bytes into lines of text, each with page number and bounding box
          (points, origin top-left). Everything downstream needs a location (rule 1).
INPUTS    PDF bytes
OUTPUTS   PdfText (pages -> lines -> words), sha256 of the file
IMPORTS   pdfplumber
NOTE      Scanned PDFs have no text layer: has_text_layer is False and no lines come
          back. OCR is NOT part of this build; such files are reported as unreadable,
          never guessed at.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Optional

import pdfplumber

MAX_PAGES = 60          # a certificate is a handful of pages; more is refused, not slowly read
LINE_TOLERANCE = 3.0     # words whose tops differ by less than this share a line


@dataclass(frozen=True)
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: Optional[float] = None      # OCR confidence 0-100; None for a real text layer


@dataclass(frozen=True)
class Line:
    page: int
    text: str
    words: tuple[Word, ...]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (min(w.x0 for w in self.words), min(w.y0 for w in self.words),
                max(w.x1 for w in self.words), max(w.y1 for w in self.words))


@dataclass(frozen=True)
class PageText:
    page: int
    width: float
    height: float
    lines: tuple[Line, ...]


@dataclass(frozen=True)
class PdfText:
    sha256: str
    pages: tuple[PageText, ...]

    @property
    def has_text_layer(self) -> bool:
        return any(p.lines for p in self.pages)

    @property
    def lines(self) -> list[Line]:
        return [ln for p in self.pages for ln in p.lines]


def read_pdf(data: bytes) -> PdfText:
    """Raises ValueError for bytes that are not a readable PDF."""
    if not data.startswith(b"%PDF"):
        raise ValueError("not a PDF file")
    pages: list[PageText] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) > MAX_PAGES:
                raise ValueError(f"PDF has more than {MAX_PAGES} pages")
            for i, page in enumerate(pdf.pages, start=1):
                words = [Word(w["text"], float(w["x0"]), float(w["top"]), float(w["x1"]), float(w["bottom"]))
                         for w in page.extract_words()]
                words.sort(key=lambda w: (w.y0, w.x0))
                rows: list[list[Word]] = []
                for w in words:
                    if rows and abs(rows[-1][0].y0 - w.y0) < LINE_TOLERANCE:
                        rows[-1].append(w)
                    else:
                        rows.append([w])
                lines = tuple(Line(i, " ".join(w.text for w in sorted(r, key=lambda w: w.x0)),
                                   tuple(sorted(r, key=lambda w: w.x0))) for r in rows)
                pages.append(PageText(i, float(page.width), float(page.height), lines))
    except Exception as e:                      # corrupt or encrypted file
        raise ValueError(f"could not read PDF: {e}") from e
    if not pages:
        raise ValueError("PDF has no pages")
    return PdfText(hashlib.sha256(data).hexdigest(), tuple(pages))
