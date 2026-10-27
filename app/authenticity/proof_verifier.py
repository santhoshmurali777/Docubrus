"""
proof_verifier.py -- decides whether a document's proof of authenticity holds.

PURPOSE   A QR code or stamp can be copied from a real document onto a fake one,
          so "a proof exists" proves nothing. This file runs four tests, in order,
          and stops at the first failure:
            1. PRESENT  the document carries a proof that can be read
            2. VALID    the QR domain is the issuer's registered domain, and the
                        signature verifies with that issuer's TRUSTED key
            3. BOUND    what the proof says (type, number, holder, key fields)
                        matches what the document itself prints
            4. FRESH    the issuer confirms it has this record, unchanged, and
                        has not revoked it
INPUTS    qr_url (or None), PrintedFacts read from the document, the trusted
          issuer list, a lookup function that asks the issuer
OUTPUTS   ProofResult: an AuthResult, the checks run, problems found, evidence
          specs (to be attached to claims), source responses, flags
IMPORTS   app/contracts.py, app/authenticity/mock_issuer.py (for the generic
          helpers canonical, parse_qr and verify_signature only)
TESTED BY tests/test_proof_verifier.py (checks run in the build log)

HOW OUTCOMES MAP (see config/statuses.md)
  no proof                         -> Proof absent
  unreadable or failed test 2 or 3 -> Proof invalid       (becomes Contradicted)
  issuer not on the trusted list,
  or document number unreadable    -> Not software-verifiable (goes to a human)
  issuer silent                    -> Issuer unreachable  (becomes Source unavailable)
  issuer has no record / revoked /
  record differs                   -> Proof invalid
  everything passes                -> Authenticated

NOT decided here: expiry (status.py does that, from valid_until) and reuse of the
same certificate in another application. Reuse can be legitimate (one society
registration backing several institutions), so it is only reported as a FLAG
and a weak evidence item, never as invalid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from app.authenticity.mock_issuer import canonical, parse_qr, verify_signature
from app.contracts import AuthResult, SourceResponse, Strength

LookupFn = Callable[[str, str], SourceResponse]        # (issuer_id, number) -> answer
SeenFn = Callable[[str, str], bool]                    # (issuer_id, number) -> used elsewhere?


@dataclass(frozen=True)
class PrintedFacts:
    """What the document itself prints, as read by the extraction pipeline."""
    doc_type: str
    number: Optional[str]
    holder: Optional[str]
    data: dict = field(default_factory=dict)     # other printed key fields, e.g. {"survey_no": "45/2"}


@dataclass(frozen=True)
class Check:
    name: str                                    # present | valid | bound | fresh
    passed: Optional[bool]                       # None = not run (an earlier test failed)
    detail: str


@dataclass(frozen=True)
class EvidenceSpec:
    """Evidence for the orchestrator to attach to this document's claims."""
    kind: str
    strength: Strength
    qualifies: bool
    detail: str
    source_response_id: Optional[str] = None


@dataclass(frozen=True)
class ProofResult:
    auth_result: AuthResult
    checks: tuple[Check, ...]
    problems: tuple[str, ...] = ()
    evidence: tuple[EvidenceSpec, ...] = ()
    source_responses: tuple[SourceResponse, ...] = ()
    flags: tuple[str, ...] = ()
    issuer_id: Optional[str] = None
    valid_until: Optional[date] = None


def _unsigned_lookup_link(qr_url: str, trusted: dict):
    """(host, issuer name) if qr_url is an unsigned link on a registered lookup-link domain, else None.
    Registered in the trusted config under 'lookup_link_domains': {host: issuer name}. A link that carries a
    signed payload (d= and s=) is not this kind and goes through the normal tests."""
    from urllib.parse import parse_qs, urlparse
    domains = trusted.get("lookup_link_domains", {})
    u = urlparse(qr_url.strip())
    q = parse_qs(u.query)
    if u.scheme != "https" or not u.hostname or ("d" in q and "s" in q):
        return None
    name = domains.get(u.hostname.lower())
    return (u.hostname.lower(), name) if name else None


def _norm(value) -> str:
    """Comparable form: case and ALL whitespace ignored (OCR often inserts or drops a space inside an identifier)."""
    return "".join(str(value).casefold().split())


# --------------------------------------------------------------------------
# The verifier
# --------------------------------------------------------------------------
def verify_proof(qr_url: Optional[str], printed: PrintedFacts, trusted: dict, lookup: LookupFn,
                 seen_elsewhere: Optional[SeenFn] = None, lookup_required: bool = True) -> ProofResult:
    """trusted is the parsed config/trusted_issuers.json (or its 'issuers' mapping)."""
    issuers = trusted.get("issuers", trusted)
    checks: list[Check] = []
    names = ("present", "valid", "bound", "fresh")

    def stop(result: AuthResult, failed: str, why: str, **kw) -> ProofResult:
        done = {c.name for c in checks}
        skipped = [Check(n, None, "not run, an earlier test failed") for n in names if n not in done and n != failed]
        checks.append(Check(failed, False, why))
        order = {n: i for i, n in enumerate(names)}
        return ProofResult(auth_result=result, checks=tuple(sorted(checks + skipped, key=lambda c: order[c.name])),
                           problems=(why,), **kw)

    # ---- 1. PRESENT
    if not qr_url or not qr_url.strip():
        return stop(AuthResult.PROOF_ABSENT, "present", "the document carries no proof that could be read")
    link = _unsigned_lookup_link(qr_url, trusted)
    if link:
        # A plain lookup link (no signature) on a registered issuer domain, e.g. AICTE's own approval letter.
        # Reading the issuer's answer is not built yet, so this can neither pass nor fail: a human decides.
        host, name = link
        checks.append(Check("present", True, f"unsigned lookup link on {host} ({name}); no signature to check"))
        return stop(AuthResult.NOT_SOFTWARE_VERIFIABLE, "valid",
                    f"the QR is a lookup link on {host} ({name}), not a signed proof; the issuer's response is not read by this version, so it needs a human check",
                    issuer_id=name, flags=("unsigned lookup link: open it and compare by hand",))
    try:
        domain, payload, signature = parse_qr(qr_url)
    except ValueError as e:
        return stop(AuthResult.PROOF_INVALID, "present", f"the proof is present but unreadable: {e}")
    checks.append(Check("present", True, f"proof read, claims to come from '{payload.get('issuer')}' via {domain}"))

    # ---- 2. VALID
    issuer_id = payload.get("issuer")
    if payload.get("v") != 1:
        return stop(AuthResult.PROOF_INVALID, "valid", f"unsupported proof version {payload.get('v')!r}", issuer_id=issuer_id)
    if issuer_id not in issuers:
        return stop(AuthResult.NOT_SOFTWARE_VERIFIABLE, "valid",
                    f"issuer '{issuer_id}' is not on the trusted issuer list, so the proof cannot be checked by software",
                    issuer_id=issuer_id)
    trust = issuers[issuer_id]
    if domain != trust["domain"]:
        return stop(AuthResult.PROOF_INVALID, "valid",
                    f"the QR points to '{domain}', not the registered domain '{trust['domain']}' of {issuer_id} (look-alike site)",
                    issuer_id=issuer_id)
    if payload.get("doc_type") not in trust["doc_types"]:
        return stop(AuthResult.PROOF_INVALID, "valid",
                    f"{issuer_id} does not issue '{payload.get('doc_type')}'", issuer_id=issuer_id)
    if not verify_signature(trust["public_key"], canonical(payload), signature):
        return stop(AuthResult.PROOF_INVALID, "valid",
                    f"the signature does not verify with {issuer_id}'s trusted key", issuer_id=issuer_id)
    checks.append(Check("valid", True, f"signature verifies with {issuer_id}'s trusted key; domain {domain} matches"))
    evidence = [EvidenceSpec("signature_valid", Strength.STRONG, True,
                             f"valid signature by {issuer_id} over number {payload['number']}")]
    valid_until = date.fromisoformat(payload["valid_until"]) if payload.get("valid_until") else None

    # ---- 3. BOUND
    if not printed.number or not str(printed.number).strip() or not printed.holder or not str(printed.holder).strip():
        return stop(AuthResult.NOT_SOFTWARE_VERIFIABLE, "bound",
                    "the document's own number or holder could not be read, so the proof cannot be matched to it",
                    issuer_id=issuer_id, valid_until=valid_until, evidence=tuple(evidence))
    mismatches = []
    if payload["doc_type"] != printed.doc_type:
        mismatches.append(f"document type: proof says '{payload['doc_type']}', document is '{printed.doc_type}'")
    if _norm(payload["number"]) != _norm(printed.number):
        mismatches.append(f"number: proof says '{payload['number']}', document prints '{printed.number}'")
    if _norm(payload["holder"]) != _norm(printed.holder):
        mismatches.append(f"holder: proof says '{payload['holder']}', document prints '{printed.holder}'")
    covered = 0
    for key, printed_value in printed.data.items():
        if key in payload.get("data", {}):
            covered += 1
            if _norm(payload["data"][key]) != _norm(printed_value):
                mismatches.append(f"{key}: proof says '{payload['data'][key]}', document prints '{printed_value}'")
    if mismatches:
        return stop(AuthResult.PROOF_INVALID, "bound",
                    "a genuine-looking proof that belongs to a different document: " + "; ".join(mismatches),
                    issuer_id=issuer_id, valid_until=valid_until, evidence=())
    checks.append(Check("bound", True, f"type, number, holder and {covered} key field(s) match what the document prints"))

    # ---- 4. FRESH
    answer = lookup(issuer_id, payload["number"])
    responses = (answer,)
    if not answer.ok:
        if lookup_required:
            return stop(AuthResult.ISSUER_UNREACHABLE, "fresh",
                        f"the issuer could not be reached to confirm the record: {answer.error}",
                        issuer_id=issuer_id, valid_until=valid_until, evidence=tuple(evidence), source_responses=responses)
        checks.append(Check("fresh", True, f"issuer unreachable ({answer.error}); not required for this document type, so the signature alone is relied on"))
        flags = ("record not confirmed with the issuer",)
        return ProofResult(AuthResult.AUTHENTICATED, tuple(checks), (), tuple(evidence), responses, flags, issuer_id, valid_until)
    rec = answer.payload
    if not rec.get("found"):
        return stop(AuthResult.PROOF_INVALID, "fresh",
                    f"the proof is validly signed but {issuer_id} has no record of number '{payload['number']}'",
                    issuer_id=issuer_id, valid_until=valid_until, evidence=(), source_responses=responses)
    if rec.get("revoked"):
        return stop(AuthResult.PROOF_INVALID, "fresh",
                    f"{issuer_id} has revoked certificate '{payload['number']}'",
                    issuer_id=issuer_id, valid_until=valid_until, evidence=(), source_responses=responses)
    issuer_view = {k: v for k, v in rec.items() if k not in ("found", "revoked")}
    if issuer_view != payload:
        return stop(AuthResult.PROOF_INVALID, "fresh",
                    f"the issuer's record for '{payload['number']}' differs from what the proof states",
                    issuer_id=issuer_id, valid_until=valid_until, evidence=(), source_responses=responses)
    checks.append(Check("fresh", True, f"{issuer_id} confirms the record, unchanged and not revoked"))
    evidence.append(EvidenceSpec("issuer_record_match", Strength.STRONG, True,
                                 f"{issuer_id} record for '{payload['number']}' matches the proof",
                                 source_response_id=answer.id))

    # ---- reuse: a flag, never a failure
    flags: list[str] = []
    if seen_elsewhere and seen_elsewhere(issuer_id, payload["number"]):
        flags.append(f"certificate '{payload['number']}' also appears in another application")
        evidence.append(EvidenceSpec("proof_reuse", Strength.WEAK, False, flags[-1]))

    return ProofResult(AuthResult.AUTHENTICATED, tuple(checks), (), tuple(evidence), responses,
                       tuple(flags), issuer_id, valid_until)
