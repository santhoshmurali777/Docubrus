"""
status.py -- the deterministic status evaluator (first line of defence).

PURPOSE   Given everything known about ONE claim, return exactly one Status and
          a reason. Implements the check order in config/statuses.md, nothing
          more and nothing less. No AI, no database, no clock: same input,
          same answer, every time.
INPUTS    EvalInput (built by the orchestrator from the ledger and checkers)
OUTPUTS   Decision (status, step, reason, cited evidence ids)
IMPORTS   app/contracts.py only
TESTED BY tests/test_status_invariant.py (checks run in the build log)

THE ORDER (first match wins; see config/statuses.md for the reasons)
  1 Missing              required claim or document absent
  2 Blurry scan          claim read from a page that failed the quality gate
  3 Expired              validity date earlier than today
  4 Contradicted         a conflict exists, or the document's proof is invalid
  5 Source unavailable   a required source failed or was never consulted,
                         or the issuer was unreachable
  6 Insufficient evidence  no strong qualifying evidence, or the document's
                         proof is absent / not software-verifiable
  7 Verified             none of the above

Verified is only reachable at step 7. If several steps apply, the first one
decides and the rest are listed in the reason as "also noted".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from app.contracts import (
    AuthResult, Claim, Evidence, Finding, SourceResponse, Status, Strength,
)

# The step each status is decided at. A test compares this table with
# config/statuses.md so code and sign-off document cannot drift apart.
STEP_STATUS: dict[int, Status] = {
    1: Status.MISSING,
    2: Status.BLURRY,
    3: Status.EXPIRED,
    4: Status.CONTRADICTED,
    5: Status.SOURCE_DOWN,
    6: Status.INSUFFICIENT,
    7: Status.VERIFIED,
}


@dataclass(frozen=True)
class Conflict:
    """One disagreement found by the contradiction engine."""
    description: str                     # e.g. "intake 120 here vs 60 in mock AICTE record"
    evidence_id: Optional[str] = None    # evidence item recording it, if any


@dataclass(frozen=True)
class EvalInput:
    """Everything known about one claim. Callers must say it all explicitly."""
    claim: Optional[Claim]               # None means the required claim was not found
    field_key: str                       # what was expected (used in the Missing reason)
    auth_result: AuthResult              # authentication result of the claim's document
    quality_ok: bool                     # did the claim's page pass the scan-quality gate?
    today: date                          # the date of the check
    valid_until: Optional[date] = None   # validity date printed on the document, if any
    conflicts: Sequence[Conflict] = field(default_factory=tuple)
    evidence: Sequence[Evidence] = field(default_factory=tuple)
    required_sources: Sequence[str] = field(default_factory=tuple)   # from the document registry
    source_responses: Sequence[SourceResponse] = field(default_factory=tuple)
    rule_version: Optional[str] = None


@dataclass(frozen=True)
class Decision:
    status: Status
    step: int
    reason: str
    evidence_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()          # other steps that also applied


# --------------------------------------------------------------------------
# Small checks, one per step condition
# --------------------------------------------------------------------------
def _failed_sources(inp: EvalInput) -> list[str]:
    """Required sources with no successful response (never consulted counts as failed)."""
    bad = []
    for name in inp.required_sources:
        mine = [r for r in inp.source_responses if r.source == name]
        if not any(r.ok for r in mine):
            if not mine:
                bad.append(f"{name} (never consulted)")
            else:
                bad.append(f"{name} ({mine[-1].error or 'failed'})")
    return bad


def _strong_qualifying(evidence: Sequence[Evidence]) -> list[Evidence]:
    return [e for e in evidence if e.strength == Strength.STRONG and e.qualifies]


# --------------------------------------------------------------------------
# The evaluator
# --------------------------------------------------------------------------
def evaluate(inp: EvalInput) -> Decision:
    # Evidence for some other claim is a caller bug. Fail loudly, never guess.
    if inp.claim is not None:
        stray = [e.id for e in inp.evidence if e.claim_id != inp.claim.id]
        if stray:
            raise ValueError(f"evidence {stray} does not belong to claim {inp.claim.id!r}")

    hits: list[tuple[int, str]] = []     # (step, explanation) for every step that applies

    # Step 1
    if inp.claim is None:
        hits.append((1, f"required '{inp.field_key}' was not found"))
        return _decide(inp, hits)        # nothing else can be judged without a claim

    # Step 2
    if not inp.quality_ok:
        hits.append((2, "the page failed the scan-quality gate, so the value cannot be relied on; ask for a clearer re-upload"))

    # Step 3 (a document valid UNTIL today is still valid today)
    if inp.valid_until is not None and inp.valid_until < inp.today:
        hits.append((3, f"validity ended {inp.valid_until.isoformat()}, before {inp.today.isoformat()}"))

    # Step 4
    problems = [c.description for c in inp.conflicts]
    if inp.auth_result == AuthResult.PROOF_INVALID:
        problems.append("the document's proof of authenticity is invalid")
    if problems:
        hits.append((4, "; ".join(problems)))

    # Step 5
    down = _failed_sources(inp)
    if inp.auth_result == AuthResult.ISSUER_UNREACHABLE:
        down.append("document issuer (unreachable)")
    if down:
        hits.append((5, "could not check: " + ", ".join(down)))

    # Step 6
    strong = _strong_qualifying(inp.evidence)
    if inp.auth_result in (AuthResult.PROOF_ABSENT, AuthResult.NOT_SOFTWARE_VERIFIABLE):
        hits.append((6, f"the document's proof is '{inp.auth_result.value}'"))
    elif inp.auth_result != AuthResult.AUTHENTICATED and not any(s == 4 or s == 5 for s, _ in hits):
        hits.append((6, f"the document is '{inp.auth_result.value}', not Authenticated"))
    if not strong:
        hits.append((6, "no strong qualifying evidence is recorded"))

    return _decide(inp, hits)


def _decide(inp: EvalInput, hits: list[tuple[int, str]]) -> Decision:
    if not hits:
        strong = _strong_qualifying(inp.evidence)
        return Decision(
            status=Status.VERIFIED, step=7,
            reason=("step 7: authenticated, read reliably, current, no conflicts, required sources "
                    f"answered, and {len(strong)} strong qualifying evidence item(s) recorded"),
            evidence_ids=tuple(e.id for e in strong),
        )

    first_step = min(s for s, _ in hits)
    status = STEP_STATUS[first_step]
    main = "; ".join(text for s, text in hits if s == first_step)
    # If the scan is unreadable, anything derived from its values is not trustworthy:
    # only list the later, reading-independent steps as notes.
    skip = {3, 4} if first_step == 2 else set()
    notes = tuple(f"step {s}: {text}" for s, text in hits if s > first_step and s not in skip)
    reason = f"step {first_step}: {main}"
    if notes:
        reason += " | also noted: " + " | ".join(notes)
    cited = tuple(e.id for e in inp.evidence) if inp.claim is not None else ()
    return Decision(status=status, step=first_step, reason=reason, evidence_ids=cited, notes=notes)


# --------------------------------------------------------------------------
# Hand-off to the ledger
# --------------------------------------------------------------------------
def to_finding(decision: Decision, claim_id: str, finding_id: str, rule_version: Optional[str] = None) -> Finding:
    """Turn a Decision into a Finding ready for ledger.record_finding."""
    return Finding(id=finding_id, claim_id=claim_id, status=decision.status, reason=decision.reason,
                   rule_version=rule_version, evidence_ids=decision.evidence_ids)
