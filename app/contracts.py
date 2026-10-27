"""
contracts.py -- the shared data contracts for the AICTE application verifier.

PURPOSE   Every other file imports these models. Nobody edits this file alone.
INPUTS    none
OUTPUTS   Status, AuthResult, Strength, DocType-agnostic models:
          Document, Claim, Evidence, SourceResponse, Rule, Finding, Receipt
IMPORTS   standard library + pydantic only
TESTED BY tests/test_contracts.py (checks run in the build log)

GOLDEN RULES enforced here (so later files cannot forget them):
  1. A Claim must point to a document, a page and a box. No location, no claim.
  2. A Finding can be VERIFIED only if it cites at least one evidence id.
  3. A Rule can be 'approved' only by someone other than its author.
  4. Weak evidence can never be marked as qualifying.
  5. Models are frozen after creation: results are recorded, never edited.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------
class Status(str, Enum):
    """Outcome for one claim. See config/statuses.md for exact definitions."""
    VERIFIED = "Verified"
    CONTRADICTED = "Contradicted"
    INSUFFICIENT = "Insufficient evidence"
    SOURCE_DOWN = "Source unavailable"
    MISSING = "Missing"
    EXPIRED = "Expired"
    BLURRY = "Blurry scan"              # read from a scan too unclear to rely on


class AuthResult(str, Enum):
    """Outcome of authenticating one DOCUMENT (its proof of authenticity)."""
    AUTHENTICATED = "Authenticated"
    PROOF_INVALID = "Proof invalid"
    PROOF_ABSENT = "Proof absent"
    ISSUER_UNREACHABLE = "Issuer unreachable"
    NOT_SOFTWARE_VERIFIABLE = "Not software-verifiable"


class Strength(str, Enum):
    """STRONG evidence may support Verified. WEAK can only raise suspicion."""
    STRONG = "strong"
    WEAK = "weak"


class RuleState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class ReviewKind(str, Enum):
    """What a human reviewer can record. None of these ever changes a Finding's status."""
    COMMENT = "comment"
    CLARIFY = "clarify"                 # ask the institution for something; stays open until resolved
    RESOLVE = "resolve"                 # closes one earlier clarification
    ATTEST = "attest"                   # "I checked this claim by hand" (a record, not a status)
    APPROVE = "approve"                 # case-level decision


# --------------------------------------------------------------------------
# Base: every record is immutable once created
# --------------------------------------------------------------------------
class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# Documents and claims
# --------------------------------------------------------------------------
class Document(Record):
    id: str
    application_id: str
    doc_type: str                       # key into config/document_registry.json
    filename: str
    sha256: str = Field(min_length=64, max_length=64)
    pages: int = Field(ge=1)
    auth_result: AuthResult = AuthResult.PROOF_ABSENT
    uploaded_at: datetime = Field(default_factory=utcnow)


class Claim(Record):
    """One extracted fact, always tied to where it was found (rule 1)."""
    id: str
    application_id: str
    document_id: str = Field(min_length=1)
    field_key: str                      # e.g. "approved_intake", "survey_no"
    raw_value: str                      # exactly as printed
    normalized_value: str               # comparable form
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float]   # x0, y0, x1, y1
    extraction_method: str              # "pdf_text" | "ocr" | "pattern" | "llm_proposed"
    ocr_confidence: Optional[float] = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _bbox_is_a_real_box(self) -> "Claim":
        x0, y0, x1, y1 = self.bbox
        if not (x1 > x0 and y1 > y0):
            raise ValueError("bbox must have x1 > x0 and y1 > y0")
        return self


# --------------------------------------------------------------------------
# Sources and evidence
# --------------------------------------------------------------------------
class SourceResponse(Record):
    """What an external (or mock) source returned, or why it failed."""
    id: str
    source: str                         # "mock_aicte", "mock_issuer:x", ...
    ok: bool
    payload: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=utcnow)
    digest: str = ""                    # hash of the payload, for the audit trail
    error: Optional[str] = None

    @model_validator(mode="after")
    def _failure_needs_reason(self) -> "SourceResponse":
        if not self.ok and not self.error:
            raise ValueError("a failed source response must carry an error")
        return self


class Evidence(Record):
    id: str
    claim_id: str
    kind: str                           # "issuer_record", "signature", "format_check", ...
    strength: Strength
    qualifies: bool                     # may this support Verified?
    detail: str
    source_response_id: Optional[str] = None

    @model_validator(mode="after")
    def _weak_never_qualifies(self) -> "Evidence":
        if self.strength == Strength.WEAK and self.qualifies:
            raise ValueError("weak evidence can never qualify a claim as verified")
        return self


# --------------------------------------------------------------------------
# Rules and findings
# --------------------------------------------------------------------------
class Rule(Record):
    id: str
    version: str
    clause_ref: str                     # e.g. "APH 2024-27, Ch.1 (to be verified)"
    illustrative: bool = True           # True until checked against the real Handbook
    effective_from: date
    state: RuleState = RuleState.DRAFT
    author: str
    approver: Optional[str] = None
    definition: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _maker_checker(self) -> "Rule":
        if self.state == RuleState.APPROVED:
            if not self.approver:
                raise ValueError("an approved rule needs an approver")
            if self.approver == self.author:
                raise ValueError("a rule's author cannot approve it")
        return self


class Finding(Record):
    id: str
    claim_id: str
    status: Status
    reason: str = Field(min_length=1)   # a finding must always explain itself
    rule_version: Optional[str] = None
    evidence_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _verified_needs_evidence(self) -> "Finding":
        if self.status == Status.VERIFIED and not self.evidence_ids:
            raise ValueError("a Verified finding must cite at least one evidence id")
        return self


# --------------------------------------------------------------------------
# Reviewer actions (human decisions, kept beside the findings, never mixed into them)
# --------------------------------------------------------------------------
class ReviewAction(Record):
    """One thing a reviewer did. Recorded through ledger.record_review_action, append-only."""
    id: str
    case_id: str
    finding_id: Optional[str] = None    # None = about the whole case
    kind: ReviewKind
    actor: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=2000)
    resolves_id: Optional[str] = None   # only for RESOLVE: the clarification being closed
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _shape(self) -> "ReviewAction":
        if not self.actor.strip():
            raise ValueError("a reviewer action needs the reviewer's name")
        if self.kind in (ReviewKind.COMMENT, ReviewKind.CLARIFY, ReviewKind.ATTEST) and not self.note.strip():
            raise ValueError(f"'{self.kind.value}' needs a written note")
        if self.kind == ReviewKind.ATTEST and not self.finding_id:
            raise ValueError("an attestation is about one finding, not the whole case")
        if self.kind == ReviewKind.APPROVE and self.finding_id:
            raise ValueError("approval is a decision about the whole case, not one finding")
        if self.kind == ReviewKind.RESOLVE and not self.resolves_id:
            raise ValueError("resolve needs the id of the clarification it closes")
        if self.kind != ReviewKind.RESOLVE and self.resolves_id:
            raise ValueError("only 'resolve' may point at an earlier action")
        return self


# --------------------------------------------------------------------------
# Receipt (what remains after a case is closed and raw files are deleted)
# --------------------------------------------------------------------------
class ReceiptItem(Record):
    doc_hash: str
    claim: str                          # "field_key=normalized_value"
    result: Status
    rule_version: Optional[str] = None
    timestamp: datetime


class Receipt(Record):
    case_id: str
    items: tuple[ReceiptItem, ...]
    signature: str = ""                 # filled in by privacy/receipts.py
