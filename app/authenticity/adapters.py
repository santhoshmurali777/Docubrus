"""
adapters.py -- ONE interface for every way a document can prove itself.

PURPOSE   The orchestrator should not care whether a proof is a mock QR link, a real digital signature, or
          (later) a live issuer API. Each way is an IssuerAdapter:
              verify(ProofInput) -> ProofResult      judge one document's proof
              lookup(number)     -> SourceResponse   ask the issuer's record book (if it has one)
          Swapping a mock for a real issuer is then a CONFIG change (the document type's proof.kind), not a code change.
ADAPTERS  MockIssuerAdapter  signed QR link + mock issuer record lookup   (proof.kind = "signed_qr")
          PadesAdapter       embedded PDF digital signature, offline      (proof.kind = "pdf_signature")
          LiveIssuerAdapter  placeholder: raises until a real issuer API is wired in
IMPORTS   proof_verifier, pades_verifier, mock_issuer, contracts
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from app.authenticity.pades_verifier import PadesVerifier
from app.authenticity.proof_verifier import PrintedFacts, ProofResult, verify_proof
from app.contracts import SourceResponse


@dataclass(frozen=True)
class ProofInput:
    """Everything an adapter may need. Each adapter uses the part it understands."""
    printed: PrintedFacts
    pdf_bytes: bytes = b""
    qr_url: Optional[str] = None


@runtime_checkable
class IssuerAdapter(Protocol):
    name: str
    def verify(self, proof: ProofInput) -> ProofResult: ...
    def lookup(self, number: str) -> SourceResponse: ...


class MockIssuerAdapter:
    name = "mock_issuer"

    def __init__(self, network, trusted: dict, seen: Optional[Callable] = None, lookup_required: bool = True):
        self.network, self.trusted, self.seen, self.lookup_required = network, trusted, seen, lookup_required

    def lookup(self, number: str, issuer_id: Optional[str] = None) -> SourceResponse:
        if issuer_id is None:
            return SourceResponse(id=f"sr_noissuer_{number}", source="mock_issuer", ok=False,
                                  error="a mock issuer lookup needs an issuer id")
        return self.network.lookup(issuer_id, number)

    def verify(self, proof: ProofInput) -> ProofResult:
        return verify_proof(proof.qr_url, proof.printed, self.trusted, self.network.lookup, self.seen, self.lookup_required)


class PadesAdapter:
    """Embedded digital signature. There is no issuer record book to ask, so lookup says so honestly (ok=False)."""
    name = "pades"

    def __init__(self, verifier: PadesVerifier):
        self.verifier = verifier

    def lookup(self, number: str) -> SourceResponse:
        return SourceResponse(id=f"sr_pades_{number}", source="pades", ok=False,
                              error="signature-based proof: this adapter has no issuer record lookup")

    def verify(self, proof: ProofInput) -> ProofResult:
        return self.verifier.verify(proof.pdf_bytes, proof.printed)


class LiveIssuerAdapter:
    """Placeholder for a real issuer API. Fails loudly instead of pretending."""
    name = "live_issuer"

    def lookup(self, number: str) -> SourceResponse:
        raise NotImplementedError("no live issuer is connected in this version")

    def verify(self, proof: ProofInput) -> ProofResult:
        raise NotImplementedError("no live issuer is connected in this version")


PROOF_KINDS = ("signed_qr", "pdf_signature")


def adapter_for(kind: str, *, network, trusted: dict, pades: PadesVerifier, seen=None, lookup_required: bool = True) -> IssuerAdapter:
    if kind == "signed_qr":
        return MockIssuerAdapter(network, trusted, seen, lookup_required)
    if kind == "pdf_signature":
        return PadesAdapter(pades)
    raise ValueError(f"no adapter for proof kind '{kind}'")
