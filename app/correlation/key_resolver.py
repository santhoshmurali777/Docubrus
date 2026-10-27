"""
key_resolver.py -- find facts that must agree across documents, and flag disagreement.

PURPOSE   For each shared key in the registry (legal name, survey no., land owner,
          built-up area, address, intake...), collect the value every document gives,
          compare using the registry's compare kind and tolerance, and report which
          claims disagree. Also checks registry relations (land owner = applicant)
          and reference records such as the mock AICTE intake.
INPUTS    claims, doc_type for each document, the parsed document_registry.json
OUTPUTS   KeyResult per shared key; conflicts_for_claim() gives status.Conflict items
IMPORTS   contracts, correlation.normalizers
TESTED BY tests/test_key_resolver.py
NOTE      Rebuilt from the registry. An unreadable value is reported as 'unreadable',
          never as agreement and never as a conflict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from app.contracts import Claim
from app.correlation.normalizers import normalize, values_agree
from app.status import Conflict


@dataclass(frozen=True)
class Reading:
    claim_id: Optional[str]        # None for a reference record (no claim)
    source: str                    # doc_type, or e.g. "mock_aicte"
    raw: str


@dataclass(frozen=True)
class Disagreement:
    key: str
    description: str
    claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class KeyResult:
    key: str
    readings: tuple[Reading, ...]
    unreadable: tuple[Reading, ...] = ()
    disagreements: tuple[Disagreement, ...] = ()

    @property
    def agrees(self) -> bool:
        return not self.disagreements


def _field_map(registry: dict) -> dict[tuple[str, str], str]:
    """(doc_type, field_key) -> shared key"""
    out = {}
    for dt, spec in registry["document_types"].items():
        for fk, f in spec["fields"].items():
            if f.get("shared_key"):
                out[(dt, fk)] = f["shared_key"]
    return out


def _clusters(kind: str, tol: Optional[dict], readings: Sequence[Reading]) -> list[list[Reading]]:
    groups: list[list[Reading]] = []
    for r in readings:
        for g in groups:
            if all(values_agree(kind, r.raw, m.raw, tol) for m in g):
                g.append(r); break
        else:
            groups.append([r])
    return groups


def resolve(claims: Sequence[Claim], doc_types: Mapping[str, str], registry: dict,
            references: Optional[Mapping[str, tuple[str, str]]] = None) -> dict[str, KeyResult]:
    """references: shared key -> (source name, value), e.g. {"approved_intake": ("mock_aicte", "60")}."""
    fmap = _field_map(registry)
    keys = registry["shared_keys"]
    by_key: dict[str, list[Reading]] = {k: [] for k in keys}
    for c in claims:
        dt = doc_types.get(c.document_id)
        sk = fmap.get((dt, c.field_key))
        if sk:
            by_key[sk].append(Reading(c.id, dt, c.raw_value))
    for k, (src, val) in (references or {}).items():
        if k in by_key:
            by_key[k].append(Reading(None, src, val))

    results: dict[str, KeyResult] = {}
    for key, spec in keys.items():
        kind, tol = spec["compare"], spec.get("tolerance")
        ok, bad = [], []
        for r in by_key[key]:
            (bad if normalize(kind, r.raw) in (None, "") else ok).append(r)
        dis: list[Disagreement] = []
        groups = _clusters(kind, tol, ok)
        if len(groups) > 1:
            desc = f"{spec['label']} differs: " + " vs ".join(
                f"'{g[0].raw}' ({', '.join(sorted({m.source for m in g}))})" for g in groups)
            ids = tuple(m.claim_id for g in groups for m in g if m.claim_id)
            dis.append(Disagreement(key, desc, ids))
        results[key] = KeyResult(key, tuple(by_key[key]), tuple(bad), tuple(dis))

    for rel in registry.get("relations", []):
        l, r = results[rel["left"]], results[rel["right"]]
        lk, rk = keys[rel["left"]], keys[rel["right"]]
        if not l.readings or not r.readings:
            continue
        mism = [(a, b) for a in l.readings for b in r.readings
                if values_agree(lk["compare"], a.raw, b.raw) is False]
        # a relation fails only if NO pairing agrees
        if mism and not any(values_agree(lk["compare"], a.raw, b.raw) for a in l.readings for b in r.readings):
            a, b = mism[0]
            ids = tuple(x.claim_id for x in (*l.readings, *r.readings) if x.claim_id)
            d = Disagreement(rel["id"], f"{rel['meaning']} Found '{a.raw}' ({a.source}) vs '{b.raw}' ({b.source}).", ids)
            results[rel["left"]] = KeyResult(l.key, l.readings, l.unreadable, l.disagreements + (d,))
    return results


def conflicts_for_claim(results: Mapping[str, KeyResult], claim_id: str) -> list[Conflict]:
    """Conflicts to hand to status.EvalInput for one claim."""
    out = []
    for kr in results.values():
        for d in kr.disagreements:
            if claim_id in d.claim_ids:
                out.append(Conflict(d.description))
    return out
