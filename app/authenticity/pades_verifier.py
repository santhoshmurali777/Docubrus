"""
pades_verifier.py -- decides whether a PDF's embedded DIGITAL SIGNATURE (PAdES / CMS) holds.

PURPOSE   Many government certificates are issued as digitally signed PDFs. Unlike a QR link, the
          signature is checked here with no help from the issuer's website. Four tests, in order,
          stopping at the first failure (the same names as proof_verifier so reports look alike):
            1. PRESENT  the PDF carries at least one embedded signature
            2. VALID    the signature is mathematically sound, the document is intact, and the
                        signer's certificate chains to a root on OUR trusted list that is registered
                        for this document type
            3. BOUND    the signature covers the WHOLE file and nothing was added after signing
                        (the printed facts we read are therefore exactly what was signed)
            4. FRESH    the signing time is not in the future and falls inside the signer
                        certificate's validity
INPUTS    PDF bytes, PrintedFacts, a PadesVerifier built from trusted roots
OUTPUTS   ProofResult (same type proof_verifier returns), so the orchestrator treats both alike
IMPORTS   pyHanko (pyhanko, pyhanko_certvalidator), app/contracts.py, app/authenticity/proof_verifier.py types
TESTED BY tests/test_pades_verifier.py

HOW OUTCOMES MAP (see config/statuses.md)
  no signature at all                          -> Proof absent
  broken structure, digest mismatch, edited
  content, bytes added after signing, signing
  time outside the certificate's life          -> Proof invalid        (becomes Contradicted)
  sound signature but signer not on our list   -> Not software-verifiable (goes to a human)
  signer on our list, but not for this doc type-> Proof invalid
  everything passes                            -> Authenticated

KNOWN LIMITS (stated in every report, never hidden)
  * Revocation (CRL / OCSP) is NOT checked: this version runs offline. A flag says so.
  * Without an embedded trusted timestamp the signing time is claimed by the signer, not proven. A flag says so.
  * The chain is validated as of today. A signer certificate that has since expired goes to a human.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from asn1crypto import pem, x509 as a1x509
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko.sign.validation.status import SignatureCoverageLevel
from pyhanko_certvalidator import ValidationContext

from app.authenticity.proof_verifier import Check, EvidenceSpec, PrintedFacts, ProofResult
from app.contracts import AuthResult, Strength

CLOCK_SKEW = timedelta(minutes=10)
_NAMES = ("present", "valid", "bound", "fresh")


@dataclass(frozen=True)
class TrustedRoot:
    issuer_id: str
    name: str
    doc_types: tuple[str, ...]
    certs: tuple                      # asn1crypto Certificates (roots)


def load_pem_certs(pem_bytes: bytes) -> tuple:
    out = []
    for _, _, der in pem.unarmor(pem_bytes, multiple=True):
        out.append(a1x509.Certificate.load(der))
    if not out:
        raise ValueError("no certificate found in the PEM data")
    return tuple(out)


def _fp(cert) -> str:
    return cert.sha256.hex() if hasattr(cert, "sha256") else ""


class PadesVerifier:
    def __init__(self, roots: list[TrustedRoot], now: Optional[datetime] = None):
        self.roots = list(roots)
        self._now = now

    @classmethod
    def from_config(cls, cfg: dict, base_dir: str, now: Optional[datetime] = None) -> "PadesVerifier":
        """cfg is the parsed config/pades_roots.json; root_pem paths are relative to base_dir."""
        import os
        roots = []
        for issuer_id, spec in (cfg.get("pades_roots") or {}).items():
            with open(os.path.join(base_dir, spec["root_pem"]), "rb") as fh:
                roots.append(TrustedRoot(issuer_id, spec.get("name", issuer_id), tuple(spec["doc_types"]), load_pem_certs(fh.read())))
        return cls(roots, now)

    # ------------------------------------------------------------------
    def verify(self, pdf_bytes: bytes, printed: PrintedFacts) -> ProofResult:
        checks: list[Check] = []

        def stop(result: AuthResult, failed: str, why: str, **kw) -> ProofResult:
            done = {c.name for c in checks}
            skipped = [Check(n, None, "not run, an earlier test failed") for n in _NAMES if n not in done and n != failed]
            checks.append(Check(failed, False, why))
            order = {n: i for i, n in enumerate(_NAMES)}
            return ProofResult(auth_result=result, checks=tuple(sorted(checks + skipped, key=lambda c: order[c.name])),
                               problems=(why,), **kw)

        now = self._now or datetime.now(timezone.utc)

        # ---- 1. PRESENT
        try:
            reader = PdfFileReader(io.BytesIO(pdf_bytes), strict=False)
            sigs = list(reader.embedded_signatures)
        except Exception as e:                                # a PDF we cannot even walk is not a proof
            return stop(AuthResult.PROOF_INVALID, "present", f"the PDF's signature structure could not be read: {type(e).__name__}")
        if not sigs:
            return stop(AuthResult.PROOF_ABSENT, "present", "the PDF carries no digital signature")
        checks.append(Check("present", True, f"{len(sigs)} embedded signature(s) found"))

        # ---- 2. VALID  (every signature must be sound; at least one must come from a trusted root for this document type)
        anchors = [c for r in self.roots for c in r.certs]
        # an empty root list must stay empty: never fall back to the operating system's trust list
        vc = ValidationContext(trust_roots=anchors, allow_fetching=False, revocation_mode="soft-fail", moment=now)
        judged = []
        for i, sig in enumerate(sigs, 1):
            try:
                st = validate_pdf_signature(sig, signer_validation_context=vc)
            except Exception as e:
                return stop(AuthResult.PROOF_INVALID, "valid", f"signature {i} could not be validated: {type(e).__name__}")
            if not (st.intact and st.valid):
                return stop(AuthResult.PROOF_INVALID, "valid",
                            f"signature {i} does not verify: the signed content was changed or the signature is damaged")
            judged.append((sig, st))
        trusted_hit = None
        for sig, st in judged:
            if not st.trusted:
                continue
            anchor = self._anchor_root(st)
            if anchor is not None:
                trusted_hit = (sig, st, anchor)
                break
        if trusted_hit is None:
            who = judged[-1][1].signing_cert.subject.human_friendly
            return stop(AuthResult.NOT_SOFTWARE_VERIFIABLE, "valid",
                        f"the signature is sound but its signer ({who}) does not chain to a trusted root on our list, so software cannot confirm who issued it")
        sig, st, root = trusted_hit
        if printed.doc_type not in root.doc_types:
            return stop(AuthResult.PROOF_INVALID, "valid", f"{root.name} is not registered to issue '{printed.doc_type}'", issuer_id=root.issuer_id)
        checks.append(Check("valid", True, f"signature is sound and chains to {root.name}'s trusted root"))
        evidence = [EvidenceSpec("signature_valid", Strength.STRONG, True,
                                 f"valid digital signature by {root.name} (certificate {st.signing_cert.sha256.hex()[:16]})")]

        # ---- 3. BOUND
        if st.coverage != SignatureCoverageLevel.ENTIRE_FILE:
            return stop(AuthResult.PROOF_INVALID, "bound",
                        "the signature does not cover the whole file: content was added after signing", issuer_id=root.issuer_id, evidence=())
        mod = getattr(st, "modification_level", None)
        if mod is not None and getattr(mod, "name", str(mod)) != "NONE":
            return stop(AuthResult.PROOF_INVALID, "bound",
                        f"the file was modified after signing (level: {getattr(mod, 'name', mod)})", issuer_id=root.issuer_id, evidence=())
        later = [s for s, _ in judged if s is not sig and s.byte_range[-2] + s.byte_range[-1] > sig.byte_range[-2] + sig.byte_range[-1]]
        checks.append(Check("bound", True, "the signature covers the entire file, so every printed value read here is covered by it"
                                          + (" (later signatures also present)" if later else "")))

        # ---- 4. FRESH
        signed_at = st.signer_reported_dt
        cert = st.signing_cert
        nbf, nae = cert.not_valid_before, cert.not_valid_after
        if signed_at is None:
            return stop(AuthResult.NOT_SOFTWARE_VERIFIABLE, "fresh", "the signature states no signing time, so its freshness cannot be judged",
                        issuer_id=root.issuer_id, evidence=tuple(evidence))
        if signed_at > now + CLOCK_SKEW:
            return stop(AuthResult.PROOF_INVALID, "fresh", f"the signing time {signed_at.isoformat()} is in the future",
                        issuer_id=root.issuer_id, evidence=())
        if not (nbf <= signed_at <= nae):
            return stop(AuthResult.PROOF_INVALID, "fresh",
                        f"signed at {signed_at.isoformat()}, outside the signer certificate's validity ({nbf.date()} to {nae.date()})",
                        issuer_id=root.issuer_id, evidence=())
        checks.append(Check("fresh", True, f"signed {signed_at.isoformat()}, inside the signer certificate's validity ({nbf.date()} to {nae.date()})"))

        flags = ["certificate revocation (CRL/OCSP) was not checked: this version runs offline"]
        evidence.append(EvidenceSpec("revocation_unchecked", Strength.WEAK, False, flags[0]))
        if getattr(st, "timestamp_validity", None) is None:
            flags.append("no trusted timestamp: the signing time is stated by the signer, not proven")
            evidence.append(EvidenceSpec("signing_time_self_reported", Strength.WEAK, False, flags[-1]))
        return ProofResult(AuthResult.AUTHENTICATED, tuple(checks), (), tuple(evidence), (), tuple(flags), root.issuer_id, None)

    def _anchor_root(self, st) -> Optional[TrustedRoot]:
        path = getattr(st, "validation_path", None)
        anchor = getattr(path, "trust_anchor", None) if path is not None else None
        cert = getattr(anchor, "certificate", None)
        if cert is None:
            return None
        fp = cert.sha256
        for r in self.roots:
            if any(c.sha256 == fp for c in r.certs):
                return r
        return None
