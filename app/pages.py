"""
pages.py -- turn PDF pages into images the reviewer can look at.

PURPOSE   Render one page to PNG and report page sizes, in the SAME units and orientation as the
          claim boxes (PDF points, origin at the top-left), so the page can draw a box over the
          exact place a claim was read from.
IMPORTS   pypdfium2 (already needed by OCR)
NOTE      pdfium is not thread-safe, so every call takes one lock. Rendered pages are cached by the
          document's sha256 so scrolling back and forth is free.
"""
from __future__ import annotations

import io
import threading
from collections import OrderedDict

import pypdfium2 as pdfium

SCALE = 2.0            # 144 dpi: readable when zoomed
MAX_SIDE_PX = 2400     # never render a huge page at full scale
CACHE_MAX = 48

_lock = threading.Lock()
_cache: "OrderedDict[tuple[str, int], bytes]" = OrderedDict()


def page_sizes(data: bytes) -> list[dict]:
    with _lock:
        doc = pdfium.PdfDocument(data)
        try:
            out = []
            for i in range(len(doc)):
                w, h = doc[i].get_size()
                out.append({"page": i + 1, "width": round(w, 2), "height": round(h, 2)})
            return out
        finally:
            doc.close()


def render_png(data: bytes, sha256: str, page: int) -> bytes:
    """`page` is 1-based. Raises IndexError when the page does not exist."""
    key = (sha256, page)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        doc = pdfium.PdfDocument(data)
        try:
            if not 1 <= page <= len(doc):
                raise IndexError(f"page {page} does not exist")
            pg = doc[page - 1]
            w, h = pg.get_size()
            scale = min(SCALE, MAX_SIDE_PX / max(w, h, 1))
            buf = io.BytesIO()
            pg.render(scale=scale).to_pil().convert("RGB").save(buf, "PNG", optimize=False)
        finally:
            doc.close()
        _cache[key] = buf.getvalue()
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
        return _cache[key]
