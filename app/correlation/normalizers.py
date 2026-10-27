"""
normalizers.py -- turn printed values into comparable form.

PURPOSE   "01/04/2026" and "1 April 2026" must compare equal; "Shri A. Rao" and
          "A RAO" must compare equal; "45 / 2" and "S.No. 45/2" too.
INPUTS    raw text as printed
OUTPUTS   normalised strings / numbers / dates, and values_agree()
IMPORTS   standard library only
TESTED BY tests/test_normalizers.py
NOTE      Rebuilt from config/document_registry.json (compare kinds: name,
          identifier, survey, number, address). Indian conventions: day first,
          lakh/crore comma grouping. Returns None when a value cannot be read:
          callers must treat None as "cannot compare", never as a match.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9


# Devanagari, Tamil, Bengali, Gujarati, Gurmukhi, Telugu, Kannada, Malayalam and Arabic-Indic digits -> ASCII
_DIGITS = {start + k: str(k) for start in (0x0966, 0x0BE6, 0x09E6, 0x0AE6, 0x0A66, 0x0C66, 0x0CE6, 0x0D66, 0x0660, 0x06F0)
           for k in range(10)}


def ascii_digits(text: Any) -> str:
    """Locale-neutral numerals: '४५/२' -> '45/2'. Identifying fields compare on ASCII digits only."""
    return str(text).translate(_DIGITS)


def _clean(text: Any) -> str:
    return " ".join(ascii_digits(text).replace(" ", " ").split())


# ---------------------------------------------------------------- dates
def normalize_date(text: Any) -> Optional[date]:
    s = _clean(text).lower().strip(" .,")
    if not s:
        return None
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", s)
    if m:                                                    # day first
        y = int(m[3]); y += 2000 if y < 100 else 0
        return _mk(y, int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{1,2})[ -/.]+([a-z]+)\.?,?[ -/.]+(\d{4})", s)
    if m and m[2] in _MONTHS:
        return _mk(int(m[3]), _MONTHS[m[2]], int(m[1]))
    m = re.fullmatch(r"([a-z]+)\.? (\d{1,2}),? (\d{4})", s)
    if m and m[1] in _MONTHS:
        return _mk(int(m[3]), _MONTHS[m[1]], int(m[2]))
    return None


def _mk(y: int, mo: int, d: int) -> Optional[date]:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


# ---------------------------------------------------------------- numbers and areas
def normalize_number(text: Any) -> Optional[float]:
    s = _clean(text).lower()
    s = re.sub(r"(rs\.?|inr|₹)", "", s)
    m = re.search(r"-?\d[\d,]*\.?\d*|-?\.\d+", s)
    if not m:
        return None
    try:
        return float(m[0].replace(",", ""))
    except ValueError:
        return None


_TO_SQM = {"sqm": 1.0, "sq m": 1.0, "sq.m": 1.0, "m2": 1.0, "m²": 1.0,
           "sqft": 0.09290304, "sq ft": 0.09290304, "sq.ft": 0.09290304, "ft2": 0.09290304,
           "hectare": 10000.0, "ha": 10000.0, "acre": 4046.8564224, "acres": 4046.8564224}


def _unit_of(s: str) -> Optional[str]:
    s = s.lower().replace("sq. ", "sq ").replace("sq.", "sq ").replace("  ", " ")
    for u in ("hectares", "hectare", "acres", "acre", "sq ft", "sqft", "sq m", "sqm", "m²", "m2", "ha"):
        if re.search(rf"\b{re.escape(u)}\b|{re.escape(u)}$", s):
            return {"hectares": "hectare"}.get(u, u.replace("sq m", "sq m"))
    return None


def area_to_sqm(text: Any) -> Optional[float]:
    n = normalize_number(text)
    u = _unit_of(_clean(text))
    if n is None or u is None:
        return None
    return n * _TO_SQM.get(u, _TO_SQM.get(u.replace(" ", ""), 0)) or None


def area_to_acres(text: Any) -> Optional[float]:
    s = _clean(text).lower()
    n = normalize_number(s)
    if n is None:
        return None
    if re.search(r"\bcents?\b", s):
        return n / 100.0
    u = _unit_of(s)
    if u is None or u in ("acre", "acres"):
        return n                                             # bare number: assume as labelled (acres)
    sqm = area_to_sqm(s)
    return sqm / 4046.8564224 if sqm else None


# ---------------------------------------------------------------- text kinds
_TITLES = {"mr", "mrs", "ms", "miss", "dr", "shri", "sri", "smt", "sh", "m/s", "ms.", "late", "prof"}


def normalize_name(text: Any) -> str:
    s = _clean(text).casefold()
    s = re.sub(r"[.,;:()'\"]", " ", s)
    toks = [t for t in s.split() if t not in _TITLES and t != "&"]
    return " ".join(toks)


def normalize_identifier(text: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", _clean(text).upper())


def normalize_survey(text: Any) -> str:
    s = _clean(text).upper()
    s = re.sub(r"^(S\.?\s*NO\.?|SURVEY\s*(NO\.?|NUMBER)?|SY\.?\s*NO\.?)\s*[:\-]?\s*", "", s)
    s = re.sub(r"\s*[/\-]\s*", "/", s)
    return re.sub(r"\s+", "", s)


_ADDR = {"rd": "road", "st": "street", "ave": "avenue", "nr": "near", "opp": "opposite",
         "no": "", "dist": "district", "tk": "taluk", "po": "", "blk": "block"}


def normalize_address(text: Any) -> str:
    s = re.sub(r"[.,;:#()\-/]", " ", _clean(text).casefold())
    toks = [_ADDR.get(t, t) for t in s.split()]
    return " ".join(t for t in toks if t)


def normalize_text(text: Any) -> str:
    return _clean(text).casefold()


# ---------------------------------------------------------------- dispatcher
def normalize(kind: str, text: Any) -> Any:
    """kind is a 'compare' value from the registry's shared_keys."""
    return {"name": normalize_name, "identifier": normalize_identifier, "survey": normalize_survey,
            "address": normalize_address, "number": normalize_number, "date": normalize_date,
            "text": normalize_text}[kind](text)


def values_agree(kind: str, a: Any, b: Any, tolerance: Optional[dict] = None) -> Optional[bool]:
    """True/False, or None when either value cannot be read (never treat None as agreement)."""
    na, nb = normalize(kind, a), normalize(kind, b)
    if na in (None, "") or nb in (None, ""):
        return None
    if kind == "number":
        tol = tolerance or {}
        diff = abs(na - nb)
        return diff <= tol.get("abs", 0) + tol.get("rel", 0) * max(abs(na), abs(nb)) + 1e-9
    if kind == "address":
        ta, tb = set(na.split()), set(nb.split())
        return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.8
    if kind == "name":
        ta, tb = na.split(), nb.split()
        if sorted(ta) == sorted(tb):
            return True
        # initials: "a rao" vs "asha rao"
        if ta and tb and ta[-1] == tb[-1]:
            f, g = ta[:-1], tb[:-1]
            return len(f) == len(g) and all(x[0] == y[0] and (len(x) == 1 or len(y) == 1 or x == y) for x, y in zip(f, g))
        return False
    return na == nb
