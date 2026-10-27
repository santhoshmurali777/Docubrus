"""
ledger.py -- the Verification Ledger: the only way results get recorded.

PURPOSE   Sit between the decision code (status.py, orchestrator) and the
          database. Every claim, piece of evidence and finding is recorded here,
          and the ledger refuses anything that would break the golden rules.
INPUTS    contract models (Claim, Evidence, SourceResponse, Finding)
OUTPUTS   recorded rows, plus read helpers for the reviewer interface
IMPORTS   app/contracts.py, app/db.py
TESTED BY tests/test_ledger.py (checks run in the build log)

WHAT THE LEDGER GUARANTEES (on top of contracts.py and db.py)
  1. Nothing is written to a CLOSED application.
  2. A claim must belong to a document of the same application.
  3. Evidence and findings must point at claims that exist.
  4. A VERIFIED finding is refused unless ALL of these hold:
       - its claim's document is Authenticated
       - it cites at least one evidence item
       - every cited evidence item belongs to that same claim
       - at least one cited item is STRONG and qualifies
     This is a second line of defence; status.py is the first.
  5. A refused write leaves nothing behind (no half-written finding).
  6. A claim can have several findings over time (re-checks); the newest wins,
     older ones stay as history.
  7. Reviewer actions (record_review_action) are append-only and live in their own table.
     They can never create or alter a Finding, so a human click cannot make a claim Verified.
     A case cannot be approved while a clarification is open, and approving a case that still
     has Contradicted / Expired / Missing items needs a written justification.

The ledger never DECIDES a status. It only records and guards.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Optional

from app import db
from app.contracts import (
    AuthResult, Claim, Evidence, Finding, ReviewAction, ReviewKind, SourceResponse, Status, Strength,
)


class LedgerError(Exception):
    """Raised when a write would break a ledger rule. Nothing is written."""


def new_id(prefix: str) -> str:
    """Short unique ids such as 'clm_3f9a1c2b7d41'."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------
def _require_open(conn, app_id: str) -> None:
    app = db.get_application(conn, app_id)
    if app is None:
        raise LedgerError(f"application {app_id!r} does not exist")
    if app["status"] != "open":
        raise LedgerError(f"application {app_id!r} is closed; nothing more can be recorded")


def _app_of_claim(conn, claim_id: str) -> str:
    claim = db.get_claim(conn, claim_id)
    if claim is None:
        raise LedgerError(f"claim {claim_id!r} does not exist")
    return claim.application_id


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------
def record_claim(conn, claim: Claim) -> Claim:
    _require_open(conn, claim.application_id)
    doc = db.get_document(conn, claim.document_id)
    if doc is None:
        raise LedgerError(f"claim {claim.id!r}: document {claim.document_id!r} does not exist")
    if doc.application_id != claim.application_id:
        raise LedgerError(f"claim {claim.id!r}: document belongs to a different application")
    if claim.page > doc.pages:
        raise LedgerError(f"claim {claim.id!r}: page {claim.page} is beyond the document's {doc.pages} pages")
    db.insert_claim(conn, claim)
    return claim


def record_source_response(conn, sr: SourceResponse, application_id: str) -> SourceResponse:
    _require_open(conn, application_id)
    db.insert_source_response(conn, sr, application_id)
    return sr


def record_evidence(conn, ev: Evidence) -> Evidence:
    _require_open(conn, _app_of_claim(conn, ev.claim_id))
    if ev.source_response_id and db.get_source_response(conn, ev.source_response_id) is None:
        raise LedgerError(f"evidence {ev.id!r}: source response {ev.source_response_id!r} does not exist")
    db.insert_evidence(conn, ev)
    return ev


def _check_verified(conn, f: Finding) -> None:
    """Extra rules that apply only to a Verified finding (guarantee 4)."""
    claim = db.get_claim(conn, f.claim_id)
    doc = db.get_document(conn, claim.document_id)
    if doc.auth_result != AuthResult.AUTHENTICATED:
        raise LedgerError(
            f"finding {f.id!r}: cannot be Verified, its document is '{doc.auth_result.value}', not Authenticated")
    if not f.evidence_ids:
        raise LedgerError(f"finding {f.id!r}: cannot be Verified without cited evidence")
    own = {e.id: e for e in db.list_evidence(conn, f.claim_id)}
    for ev_id in f.evidence_ids:
        if ev_id not in own:
            raise LedgerError(f"finding {f.id!r}: evidence {ev_id!r} is not recorded for this claim")
    if not any(own[i].strength == Strength.STRONG and own[i].qualifies for i in f.evidence_ids):
        raise LedgerError(f"finding {f.id!r}: cannot be Verified, none of its evidence is strong and qualifying")


def record_finding(conn, f: Finding) -> Finding:
    _require_open(conn, _app_of_claim(conn, f.claim_id))
    # Any cited evidence must belong to this claim, whatever the status.
    own_ids = {e.id for e in db.list_evidence(conn, f.claim_id)}
    stray = [i for i in f.evidence_ids if i not in own_ids]
    if stray:
        raise LedgerError(f"finding {f.id!r}: cites evidence not recorded for its claim: {stray}")
    if f.status == Status.VERIFIED:
        _check_verified(conn, f)
    db.insert_finding(conn, f)
    return f


# --------------------------------------------------------------------------
# Reading (for the reviewer interface and the coverage map)
# --------------------------------------------------------------------------
def latest_finding(conn, claim_id: str) -> Optional[Finding]:
    row = conn.execute(
        "SELECT id FROM findings WHERE claim_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (claim_id,)).fetchone()
    return db.get_finding(conn, row["id"]) if row else None


def finding_history(conn, claim_id: str) -> list[Finding]:
    rows = conn.execute(
        "SELECT id FROM findings WHERE claim_id = ? ORDER BY created_at, rowid", (claim_id,)).fetchall()
    return [db.get_finding(conn, r["id"]) for r in rows]


def current_findings(conn, app_id: str) -> list[Finding]:
    """The newest finding for each claim of an application."""
    out = []
    for claim in db.list_claims(conn, app_id):
        f = latest_finding(conn, claim.id)
        if f:
            out.append(f)
    return out


def status_counts(conn, app_id: str) -> dict[str, int]:
    """How many claims sit in each status (feeds the coverage map)."""
    counts = {s.value: 0 for s in Status}
    for f in current_findings(conn, app_id):
        counts[f.status.value] += 1
    return counts


def explain(conn, finding_id: str) -> str:
    """Plain-language trace a reviewer can read: claim, location, evidence, why."""
    t = db.trace(conn, finding_id)
    if t is None:
        raise LedgerError(f"finding {finding_id!r} does not exist")
    f, c, d = t["finding"], t["claim"], t["document"]
    lines = [
        f"Finding: {f.status.value}",
        f"Reason : {f.reason}",
        f"Claim  : {c.field_key} = {c.raw_value!r} (normalised {c.normalized_value!r})",
        f"Where  : {d.filename}, page {c.page}, box {c.bbox}",
        f"Proof  : document is '{d.auth_result.value}'",
        f"Rule   : version {f.rule_version or 'none'}",
    ]
    cited = set(f.evidence_ids)
    for e in t["evidence"]:
        mark = "cited" if e.id in cited else "recorded"
        lines.append(f"Evidence ({mark}): [{e.strength.value}{', qualifies' if e.qualifies else ''}] {e.kind}: {e.detail}")
    for s in t["sources"]:
        lines.append(f"Source : {s.source} {'ok' if s.ok else 'FAILED: ' + str(s.error)} at {s.checked_at:%Y-%m-%d %H:%M}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Reviewer actions (W5)
# --------------------------------------------------------------------------
def record_review_action(conn, a: ReviewAction, *, case_finding_ids, attention: int = 0) -> ReviewAction:
    """Record what a reviewer did.

    case_finding_ids  the finding ids that belong to this case (the caller owns the report)
    attention         how many Contradicted / Expired / Missing items the case has right now
    """
    if a.finding_id is not None and a.finding_id not in set(case_finding_ids):
        raise LedgerError(f"finding {a.finding_id!r} does not belong to case {a.case_id!r}")
    history = db.list_review_actions(conn, a.case_id)
    if a.kind == ReviewKind.RESOLVE:
        target = next((x for x in history if x.id == a.resolves_id), None)
        if target is None or target.kind != ReviewKind.CLARIFY:
            raise LedgerError("resolve must point at an existing clarification of this case")
        if any(x.kind == ReviewKind.RESOLVE and x.resolves_id == target.id for x in history):
            raise LedgerError("that clarification is already resolved")
        if a.finding_id != target.finding_id:
            raise LedgerError("resolve must be about the same finding as the clarification")
    if a.kind == ReviewKind.APPROVE:
        if review_state(conn, a.case_id)["open_clarifications"]:
            raise LedgerError("cannot approve while a clarification is still open")
        if attention and not a.note.strip():
            raise LedgerError(f"this case still has {attention} contradicted, expired or missing item(s); "
                              "approving it needs a written justification")
    db.insert_review_action(conn, a)
    return a


def review_state(conn, case_id: str) -> dict:
    """Where a case stands from the reviewer's side. Derived from the action trail, never stored."""
    acts = db.list_review_actions(conn, case_id)
    resolved = {x.resolves_id for x in acts if x.kind == ReviewKind.RESOLVE}
    open_cl = [x for x in acts if x.kind == ReviewKind.CLARIFY and x.id not in resolved]
    by_finding: dict[str, dict] = {}
    for x in acts:
        if not x.finding_id:
            continue
        d = by_finding.setdefault(x.finding_id, {"comments": 0, "open_clarifications": 0, "attested": False})
        if x.kind == ReviewKind.COMMENT:
            d["comments"] += 1
        elif x.kind == ReviewKind.ATTEST:
            d["attested"] = True
        elif x.kind == ReviewKind.CLARIFY and x.id not in resolved:
            d["open_clarifications"] += 1
    last_decision = next((x for x in reversed(acts) if x.kind in (ReviewKind.APPROVE, ReviewKind.CLARIFY)), None)
    if open_cl:
        state = "clarification_requested"
    elif last_decision and last_decision.kind == ReviewKind.APPROVE:
        state = "approved"
    elif acts:
        state = "in_review"
    else:
        state = "new"
    return {"case": state, "open_clarifications": len(open_cl), "actions": len(acts), "by_finding": by_finding,
            "resolved_ids": sorted(resolved)}
