"""
reuse.py -- the same land or certificate used in another application.

PURPOSE   Find (a) a certificate number already presented in another application and
          (b) a survey number already claimed by another application, unless the
          institution declared the sharing. Reuse is a SIGN to look closer, so it is
          returned as a flag / weak evidence, never as a proof of wrongdoing.
INPUTS    sqlite connection, the application id, registry, optional declared set
OUTPUTS   list[ReuseFinding]; seen_elsewhere_fn() for proof_verifier's SeenFn
IMPORTS   db, correlation.normalizers
TESTED BY tests/test_reuse.py
NOTE      Reads other applications only to match identifiers. It returns the other
          application's id and nothing else about it. Closed applications are
          included: their ledger rows stay, raw files do not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from app.correlation.normalizers import normalize_identifier, normalize_survey


@dataclass(frozen=True)
class ReuseFinding:
    kind: str                       # "certificate" | "land"
    key: str                        # normalised number / survey number
    claim_id: str
    other_application_id: str
    description: str


def _fetch(conn, app_id: str, keys: tuple[str, ...], same: bool):
    marks = ",".join("?" for _ in keys)
    op = "=" if same else "<>"
    return conn.execute(
        f"SELECT c.id, c.application_id, c.field_key, c.raw_value, d.doc_type FROM claims c "
        f"JOIN documents d ON d.id = c.document_id WHERE c.field_key IN ({marks}) AND c.application_id {op} ?",
        (*keys, app_id)).fetchall()


def find_reuse(conn, app_id: str, declared_shared: Optional[Iterable[str]] = None) -> list[ReuseFinding]:
    declared = {normalize_survey(x) for x in (declared_shared or [])}
    out: list[ReuseFinding] = []

    mine = _fetch(conn, app_id, ("document_number",), True)
    theirs = _fetch(conn, app_id, ("document_number",), False)
    seen = {(r["doc_type"], normalize_identifier(r["raw_value"])): r["application_id"] for r in theirs if normalize_identifier(r["raw_value"])}
    for r in mine:
        k = (r["doc_type"], normalize_identifier(r["raw_value"]))
        if k in seen:
            out.append(ReuseFinding("certificate", k[1], r["id"], seen[k],
                                    f"{r['doc_type']} number '{r['raw_value']}' also appears in application {seen[k]}"))

    mine = _fetch(conn, app_id, ("survey_no",), True)
    theirs = _fetch(conn, app_id, ("survey_no",), False)
    land = {}
    for r in theirs:
        if r["doc_type"] in ("land_deed", "land_use_certificate", "encumbrance_certificate"):
            land.setdefault(normalize_survey(r["raw_value"]), r["application_id"])
    done = set()
    for r in mine:
        s = normalize_survey(r["raw_value"])
        if r["doc_type"] in ("land_deed",) and s in land and s not in declared and s not in done:
            done.add(s)
            out.append(ReuseFinding("land", s, r["id"], land[s],
                                    f"survey number '{r['raw_value']}' is also claimed by application {land[s]} and the sharing was not declared"))
    return out


def seen_elsewhere_fn(conn, app_id: str, registry: dict) -> Callable[[str, str], bool]:
    """Adapter for proof_verifier.verify_proof(seen_elsewhere=...): (issuer_id, number) -> bool."""
    issued_by: dict[str, set[str]] = {}
    for dt, spec in registry["document_types"].items():
        iss = spec.get("proof", {}).get("issuer_id")
        if iss:
            issued_by.setdefault(iss, set()).add(dt)

    def seen(issuer_id: str, number: str) -> bool:
        types = issued_by.get(issuer_id, set())
        want = normalize_identifier(number)
        return any(r["doc_type"] in types and normalize_identifier(r["raw_value"]) == want
                   for r in _fetch(conn, app_id, ("document_number",), False))
    return seen
