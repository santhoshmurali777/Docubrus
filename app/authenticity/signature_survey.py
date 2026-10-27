"""
signature_survey.py -- which of MY real PDFs actually carry an embedded digital signature?

PURPOSE   Answer, before anyone switches a document type to proof.kind = "pdf_signature", whether the real
          certificates of that type ship signed at all, who signs them, and whether the signature is intact.
RUN       python -m app.authenticity.signature_survey <file-or-folder> [...] [--json]
OUTPUT    one row per PDF (signed or not, how many signatures, signer, issuer, signing time, intact,
          covers whole file), then a summary of distinct issuers.
NOTE      This tool trusts NOTHING. It does not decide Authenticated; that needs a root certificate you got
          from the issuing authority itself (never one lifted out of the sample PDF, which would be circular).
          Use the "issuer" lines to know which authority's root to ask for, then follow the README (W2).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
from collections import Counter

from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext


# pyHanko logs a long traceback whenever a signer's chain cannot be built. Here that is the expected, boring case.
for _n in ("pyhanko", "pyhanko_certvalidator"):
    logging.getLogger(_n).setLevel(logging.CRITICAL)


def _cn(name) -> str:
    try:
        return name.human_friendly
    except Exception:
        return "?"


def survey_pdf(data: bytes) -> dict:
    """Describe the signatures in one PDF. Never raises: a broken file is reported as such."""
    if not data.startswith(b"%PDF"):
        return {"signed": False, "problem": "not a PDF file", "signatures": []}
    try:
        sigs = list(PdfFileReader(io.BytesIO(data), strict=False).embedded_signatures)
    except Exception as e:
        return {"signed": False, "problem": f"signature structure unreadable ({type(e).__name__})", "signatures": []}
    vc = ValidationContext(trust_roots=[], allow_fetching=False, revocation_mode="soft-fail")   # trust nothing
    rows = []
    for s in sigs:
        row = {"field": getattr(s, "field_name", "?")}
        try:
            st = validate_pdf_signature(s, signer_validation_context=vc)
            cert = st.signing_cert
            when = st.signer_reported_dt
            row.update(signer=_cn(cert.subject), issuer=_cn(cert.issuer), self_signed=cert.subject == cert.issuer,
                       signed_at=when.isoformat() if when else None, intact=bool(st.intact and st.valid),
                       whole_file=st.coverage == SignatureCoverageLevel.ENTIRE_FILE,
                       cert_sha256=cert.sha256.hex()[:16],
                       cert_valid=f"{cert.not_valid_before.date()} to {cert.not_valid_after.date()}")
        except Exception as e:
            row.update(problem=f"could not be read ({type(e).__name__})", intact=False)
        rows.append(row)
    return {"signed": bool(rows), "signatures": rows}


def collect(paths: list[str]) -> list[str]:
    out = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                out += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(".pdf")]
        elif os.path.isfile(p):
            out.append(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Report which PDFs carry embedded digital signatures.")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    files = collect(a.paths)
    if not files:
        print("no PDF files found", file=sys.stderr)
        return 2
    results = []
    for f in files:
        with open(f, "rb") as fh:
            results.append({"file": f, **survey_pdf(fh.read())})
    if a.json:
        print(json.dumps(results, indent=2))
        return 0
    for r in results:
        if not r["signed"]:
            print(f"UNSIGNED  {r['file']}" + (f"  ({r['problem']})" if r.get("problem") else ""))
            continue
        for s in r["signatures"]:
            if "problem" in s:
                print(f"SIGNED?   {r['file']}  {s['problem']}")
                continue
            flags = ["intact" if s["intact"] else "BROKEN", "whole file" if s["whole_file"] else "PARTIAL COVERAGE"]
            if s["self_signed"]:
                flags.append("self-signed")
            print(f"SIGNED    {r['file']}\n          signer: {s['signer']}\n          issuer: {s['issuer']}"
                  f"\n          signed: {s['signed_at'] or 'no signing time stated'}   cert {s['cert_valid']}   [{', '.join(flags)}]")
    n_signed = sum(r["signed"] for r in results)
    print(f"\n{n_signed} of {len(results)} file(s) carry an embedded signature.")
    issuers = Counter(s["issuer"] for r in results for s in r["signatures"] if "issuer" in s)
    if issuers:
        print("Issuers seen (ask each authority for its ROOT certificate; do not use one taken from these PDFs):")
        for i, n in issuers.most_common():
            print(f"  {n} x {i}")
    else:
        print("None are signed, so proof.kind = \"pdf_signature\" is not usable for these documents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
