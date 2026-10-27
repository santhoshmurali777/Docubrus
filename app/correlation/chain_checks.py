"""
chain_checks.py -- the approval chain must exist and be in date order.

PURPOSE   Land deed -> land use certificate -> building plan -> occupancy certificate
          (the registry's date_chain). A later document needs every earlier one to be
          present, and each date must be on or after the previous one.
INPUTS    claims, doc_type for each document, parsed document_registry.json
OUTPUTS   list[ChainIssue]; issues_for_claim() gives status.Conflict items
IMPORTS   contracts, correlation.normalizers, status
TESTED BY tests/test_chain.py
NOTE      Rebuilt from the registry. A date that cannot be read is reported as its
          own issue ('unreadable'), never silently skipped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from app.contracts import Claim
from app.correlation.normalizers import normalize_date
from app.status import Conflict


@dataclass(frozen=True)
class ChainIssue:
    kind: str                      # "missing" | "order" | "unreadable"
    doc_type: str
    description: str
    claim_ids: tuple[str, ...] = ()


def check_chain(claims: Sequence[Claim], doc_types: Mapping[str, str], registry: dict) -> list[ChainIssue]:
    steps = registry["date_chain"]["steps"]
    present = set(doc_types.values())
    issues: list[ChainIssue] = []
    found = []                                     # (step, claim, date) in chain order
    for s in steps:
        c = next((c for c in claims if doc_types.get(c.document_id) == s["doc_type"] and c.field_key == s["field"]), None)
        found.append((s, c, normalize_date(c.raw_value) if c else None))

    last_present = max((i for i, (s, _, _) in enumerate(found) if s["doc_type"] in present), default=-1)
    for i, (s, c, d) in enumerate(found):
        label = registry["document_types"][s["doc_type"]]["label"]
        if s["doc_type"] not in present and i < last_present:
            issues.append(ChainIssue("missing", s["doc_type"],
                                     f"{label} is missing, but a later document in the approval chain cites it"))
        elif s["doc_type"] in present and c is not None and d is None:
            issues.append(ChainIssue("unreadable", s["doc_type"], f"the {s['field']} on the {label} could not be read as a date", (c.id,)))

    readable = [(s, c, d) for s, c, d in found if c is not None and d is not None]
    for (s1, c1, d1), (s2, c2, d2) in zip(readable, readable[1:]):
        if d2 < d1:
            l1 = registry["document_types"][s1["doc_type"]]["label"]; l2 = registry["document_types"][s2["doc_type"]]["label"]
            issues.append(ChainIssue("order", s2["doc_type"],
                                     f"{l2} is dated {d2.isoformat()}, before the {l1} it depends on ({d1.isoformat()})",
                                     (c1.id, c2.id)))
    return issues


def issues_for_claim(issues: Sequence[ChainIssue], claim_id: str) -> list[Conflict]:
    return [Conflict(i.description) for i in issues if i.kind == "order" and claim_id in i.claim_ids]
