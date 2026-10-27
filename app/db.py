"""
db.py -- SQLite storage for the contract models.

PURPOSE   Create the tables and provide small save/load functions for every
          model in contracts.py. No decision logic lives here.
INPUTS    contract models (Document, Claim, Evidence, ...)
OUTPUTS   the same contract models, rebuilt from rows
IMPORTS   app/contracts.py, standard library (sqlite3, json)
TESTED BY tests/test_db.py (checks run in the build log)

DESIGN NOTES
  * Ledger tables (claims, evidence, source_responses, findings) are APPEND-ONLY:
    database triggers block UPDATE, so a recorded result can never be edited.
  * documents, rules and applications are mutable (authentication result is set
    after upload, a rule moves from draft to approved, a case gets closed).
  * Foreign keys are ON: you cannot record a claim for a missing document, or
    evidence for a missing claim.
  * purge_application() is the ONLY way raw/derived data is removed. It is used
    by privacy/close_case.py. Receipts are deliberately kept.
  * Consent records get their own table later (authenticity/consent.py) using
    the same connection.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Optional

from app.contracts import (
    AuthResult, Claim, Document, Evidence, Finding, Receipt, ReceiptItem,
    ReviewAction, ReviewKind, Rule, RuleState, SourceResponse, Status, Strength, utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id               TEXT PRIMARY KEY,
    category         TEXT NOT NULL,
    institution_name TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    created_at       TEXT NOT NULL,
    closed_at        TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id             TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    doc_type       TEXT NOT NULL,
    filename       TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    pages          INTEGER NOT NULL CHECK (pages >= 1),
    auth_result    TEXT NOT NULL,
    uploaded_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id                TEXT PRIMARY KEY,
    application_id    TEXT NOT NULL REFERENCES applications(id),
    document_id       TEXT NOT NULL REFERENCES documents(id),
    field_key         TEXT NOT NULL,
    raw_value         TEXT NOT NULL,
    normalized_value  TEXT NOT NULL,
    page              INTEGER NOT NULL CHECK (page >= 1),
    x0 REAL NOT NULL, y0 REAL NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL,
    extraction_method TEXT NOT NULL,
    ocr_confidence    REAL
);

CREATE TABLE IF NOT EXISTS source_responses (
    id             TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    source         TEXT NOT NULL,
    ok             INTEGER NOT NULL CHECK (ok IN (0,1)),
    payload        TEXT NOT NULL,
    checked_at     TEXT NOT NULL,
    digest         TEXT NOT NULL,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
    id                 TEXT PRIMARY KEY,
    claim_id           TEXT NOT NULL REFERENCES claims(id),
    kind               TEXT NOT NULL,
    strength           TEXT NOT NULL CHECK (strength IN ('strong','weak')),
    qualifies          INTEGER NOT NULL CHECK (qualifies IN (0,1)),
    detail             TEXT NOT NULL,
    source_response_id TEXT REFERENCES source_responses(id),
    CHECK (NOT (strength = 'weak' AND qualifies = 1))
);

CREATE TABLE IF NOT EXISTS rules (
    id             TEXT NOT NULL,
    version        TEXT NOT NULL,
    clause_ref     TEXT NOT NULL,
    illustrative   INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    state          TEXT NOT NULL CHECK (state IN ('draft','approved')),
    author         TEXT NOT NULL,
    approver       TEXT,
    definition     TEXT NOT NULL,
    PRIMARY KEY (id, version),
    CHECK (state = 'draft' OR (approver IS NOT NULL AND approver <> author))
);

CREATE TABLE IF NOT EXISTS findings (
    id           TEXT PRIMARY KEY,
    claim_id     TEXT NOT NULL REFERENCES claims(id),
    status       TEXT NOT NULL,
    reason       TEXT NOT NULL CHECK (length(reason) > 0),
    rule_version TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_evidence (
    finding_id  TEXT NOT NULL REFERENCES findings(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    position    INTEGER NOT NULL,
    PRIMARY KEY (finding_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS review_actions (
    id          TEXT PRIMARY KEY,
    case_id     TEXT NOT NULL,
    finding_id  TEXT,
    kind        TEXT NOT NULL CHECK (kind IN ('comment','clarify','resolve','attest','approve')),
    actor       TEXT NOT NULL CHECK (length(actor) > 0),
    note        TEXT NOT NULL,
    resolves_id TEXT REFERENCES review_actions(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_actions_case ON review_actions(case_id);

CREATE TABLE IF NOT EXISTS receipts (
    case_id    TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    signature  TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

# Ledger tables may be inserted into and read, never edited.
_APPEND_ONLY = ["claims", "evidence", "source_responses", "findings"]


def _triggers() -> str:
    out = [
        f"""CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
            BEGIN SELECT RAISE(ABORT, '{t} is append-only: recorded results cannot be edited'); END;"""
        for t in _APPEND_ONLY
    ]
    # Reviewer actions are an audit trail: neither edited nor deleted.
    out.append("""CREATE TRIGGER IF NOT EXISTS review_actions_no_update BEFORE UPDATE ON review_actions
            BEGIN SELECT RAISE(ABORT, 'review_actions is append-only: a recorded decision cannot be edited'); END;""")
    out.append("""CREATE TRIGGER IF NOT EXISTS review_actions_no_delete BEFORE DELETE ON review_actions
            BEGIN SELECT RAISE(ABORT, 'review_actions is append-only: a recorded decision cannot be deleted'); END;""")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Connection and setup
# --------------------------------------------------------------------------
def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")   # deleted rows are overwritten, not just unlinked
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(_triggers())
    conn.commit()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


# --------------------------------------------------------------------------
# Applications
# --------------------------------------------------------------------------
def create_application(conn, app_id: str, category: str, institution_name: str) -> None:
    conn.execute(
        "INSERT INTO applications (id, category, institution_name, created_at) VALUES (?,?,?,?)",
        (app_id, category, institution_name, utcnow().isoformat()),
    )
    conn.commit()


def get_application(conn, app_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    return dict(row) if row else None


def mark_application_closed(conn, app_id: str) -> None:
    conn.execute(
        "UPDATE applications SET status = 'closed', closed_at = ? WHERE id = ?",
        (utcnow().isoformat(), app_id),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------
def insert_document(conn, d: Document) -> None:
    conn.execute(
        "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
        (d.id, d.application_id, d.doc_type, d.filename, d.sha256, d.pages,
         d.auth_result.value, d.uploaded_at.isoformat()),
    )
    conn.commit()


def set_document_auth(conn, doc_id: str, result: AuthResult) -> None:
    """Authentication happens after upload, so this one field may change."""
    conn.execute("UPDATE documents SET auth_result = ? WHERE id = ?", (result.value, doc_id))
    conn.commit()


def _document(r) -> Document:
    return Document(id=r["id"], application_id=r["application_id"], doc_type=r["doc_type"],
                    filename=r["filename"], sha256=r["sha256"], pages=r["pages"],
                    auth_result=AuthResult(r["auth_result"]), uploaded_at=_dt(r["uploaded_at"]))


def get_document(conn, doc_id: str) -> Optional[Document]:
    r = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return _document(r) if r else None


def list_documents(conn, app_id: str) -> list[Document]:
    rows = conn.execute("SELECT * FROM documents WHERE application_id = ? ORDER BY uploaded_at, id", (app_id,))
    return [_document(r) for r in rows]


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------
def insert_claim(conn, c: Claim) -> None:
    x0, y0, x1, y1 = c.bbox
    conn.execute(
        "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (c.id, c.application_id, c.document_id, c.field_key, c.raw_value, c.normalized_value,
         c.page, x0, y0, x1, y1, c.extraction_method, c.ocr_confidence),
    )
    conn.commit()


def _claim(r) -> Claim:
    return Claim(id=r["id"], application_id=r["application_id"], document_id=r["document_id"],
                 field_key=r["field_key"], raw_value=r["raw_value"], normalized_value=r["normalized_value"],
                 page=r["page"], bbox=(r["x0"], r["y0"], r["x1"], r["y1"]),
                 extraction_method=r["extraction_method"], ocr_confidence=r["ocr_confidence"])


def get_claim(conn, claim_id: str) -> Optional[Claim]:
    r = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    return _claim(r) if r else None


def list_claims(conn, app_id: str) -> list[Claim]:
    return [_claim(r) for r in conn.execute(
        "SELECT * FROM claims WHERE application_id = ? ORDER BY id", (app_id,))]


# --------------------------------------------------------------------------
# Source responses and evidence
# --------------------------------------------------------------------------
def insert_source_response(conn, s: SourceResponse, application_id: str) -> None:
    conn.execute(
        "INSERT INTO source_responses VALUES (?,?,?,?,?,?,?,?)",
        (s.id, application_id, s.source, int(s.ok), json.dumps(s.payload, sort_keys=True),
         s.checked_at.isoformat(), s.digest, s.error),
    )
    conn.commit()


def get_source_response(conn, sr_id: str) -> Optional[SourceResponse]:
    r = conn.execute("SELECT * FROM source_responses WHERE id = ?", (sr_id,)).fetchone()
    if not r:
        return None
    return SourceResponse(id=r["id"], source=r["source"], ok=bool(r["ok"]),
                          payload=json.loads(r["payload"]), checked_at=_dt(r["checked_at"]),
                          digest=r["digest"], error=r["error"])


def insert_evidence(conn, e: Evidence) -> None:
    conn.execute(
        "INSERT INTO evidence VALUES (?,?,?,?,?,?,?)",
        (e.id, e.claim_id, e.kind, e.strength.value, int(e.qualifies), e.detail, e.source_response_id),
    )
    conn.commit()


def _evidence(r) -> Evidence:
    return Evidence(id=r["id"], claim_id=r["claim_id"], kind=r["kind"], strength=Strength(r["strength"]),
                    qualifies=bool(r["qualifies"]), detail=r["detail"],
                    source_response_id=r["source_response_id"])


def list_evidence(conn, claim_id: str) -> list[Evidence]:
    return [_evidence(r) for r in conn.execute(
        "SELECT * FROM evidence WHERE claim_id = ? ORDER BY id", (claim_id,))]


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
def save_rule(conn, rule: Rule) -> None:
    """Insert or replace one rule version (used when a draft is approved)."""
    conn.execute(
        "INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,?,?,?,?)",
        (rule.id, rule.version, rule.clause_ref, int(rule.illustrative), rule.effective_from.isoformat(),
         rule.state.value, rule.author, rule.approver, json.dumps(rule.definition, sort_keys=True)),
    )
    conn.commit()


def _rule(r) -> Rule:
    return Rule(id=r["id"], version=r["version"], clause_ref=r["clause_ref"],
                illustrative=bool(r["illustrative"]), effective_from=date.fromisoformat(r["effective_from"]),
                state=RuleState(r["state"]), author=r["author"], approver=r["approver"],
                definition=json.loads(r["definition"]))


def list_rule_versions(conn, rule_id: str) -> list[Rule]:
    return [_rule(r) for r in conn.execute(
        "SELECT * FROM rules WHERE id = ? ORDER BY effective_from, version", (rule_id,))]


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------
def insert_finding(conn, f: Finding) -> None:
    conn.execute(
        "INSERT INTO findings VALUES (?,?,?,?,?,?)",
        (f.id, f.claim_id, f.status.value, f.reason, f.rule_version, f.created_at.isoformat()),
    )
    for pos, ev_id in enumerate(f.evidence_ids):
        conn.execute("INSERT INTO finding_evidence VALUES (?,?,?)", (f.id, ev_id, pos))
    conn.commit()


def _finding(conn, r) -> Finding:
    ev = [x["evidence_id"] for x in conn.execute(
        "SELECT evidence_id FROM finding_evidence WHERE finding_id = ? ORDER BY position", (r["id"],))]
    return Finding(id=r["id"], claim_id=r["claim_id"], status=Status(r["status"]), reason=r["reason"],
                   rule_version=r["rule_version"], evidence_ids=tuple(ev), created_at=_dt(r["created_at"]))


def get_finding(conn, finding_id: str) -> Optional[Finding]:
    r = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    return _finding(conn, r) if r else None


def list_findings(conn, app_id: str) -> list[Finding]:
    rows = conn.execute(
        "SELECT f.* FROM findings f JOIN claims c ON c.id = f.claim_id "
        "WHERE c.application_id = ? ORDER BY f.created_at, f.id", (app_id,))
    return [_finding(conn, r) for r in rows]


def trace(conn, finding_id: str) -> Optional[dict]:
    """One query path from a finding back to its claim, evidence and sources."""
    f = get_finding(conn, finding_id)
    if not f:
        return None
    claim = get_claim(conn, f.claim_id)
    evidence = list_evidence(conn, f.claim_id)
    sources = [get_source_response(conn, e.source_response_id)
               for e in evidence if e.source_response_id]
    return {"finding": f, "claim": claim, "evidence": evidence,
            "sources": sources, "document": get_document(conn, claim.document_id)}


# --------------------------------------------------------------------------
# Reviewer actions
# --------------------------------------------------------------------------
def insert_review_action(conn, a: ReviewAction) -> None:
    conn.execute(
        "INSERT INTO review_actions VALUES (?,?,?,?,?,?,?,?)",
        (a.id, a.case_id, a.finding_id, a.kind.value, a.actor.strip(), a.note.strip(), a.resolves_id,
         a.created_at.isoformat()),
    )
    conn.commit()


def _review_action(r) -> ReviewAction:
    return ReviewAction(id=r["id"], case_id=r["case_id"], finding_id=r["finding_id"], kind=ReviewKind(r["kind"]),
                        actor=r["actor"], note=r["note"], resolves_id=r["resolves_id"], created_at=_dt(r["created_at"]))


def get_review_action(conn, action_id: str) -> Optional[ReviewAction]:
    r = conn.execute("SELECT * FROM review_actions WHERE id = ?", (action_id,)).fetchone()
    return _review_action(r) if r else None


def list_review_actions(conn, case_id: str) -> list[ReviewAction]:
    """Oldest first, in the order they were recorded."""
    return [_review_action(r) for r in conn.execute(
        "SELECT * FROM review_actions WHERE case_id = ? ORDER BY rowid", (case_id,))]


# --------------------------------------------------------------------------
# Receipts and purge (used by privacy/close_case.py)
# --------------------------------------------------------------------------
def save_receipt(conn, r: Receipt) -> None:
    body = json.dumps([i.model_dump(mode="json") for i in r.items], sort_keys=True)
    conn.execute("INSERT OR REPLACE INTO receipts VALUES (?,?,?,?)",
                 (r.case_id, body, r.signature, utcnow().isoformat()))
    conn.commit()


def get_receipt(conn, case_id: str) -> Optional[Receipt]:
    r = conn.execute("SELECT * FROM receipts WHERE case_id = ?", (case_id,)).fetchone()
    if not r:
        return None
    items = tuple(ReceiptItem(**i) for i in json.loads(r["body"]))
    return Receipt(case_id=r["case_id"], items=items, signature=r["signature"])


def purge_application(conn, app_id: str) -> dict:
    """Delete every document, claim, evidence, source response and finding of
    one application. Keeps the application row (marked closed) and its receipt."""
    q = lambda sql: conn.execute(sql, (app_id,)).rowcount
    counts = {
        "finding_evidence": q("DELETE FROM finding_evidence WHERE finding_id IN (SELECT f.id FROM findings f JOIN claims c ON c.id=f.claim_id WHERE c.application_id = ?)"),
        "findings": q("DELETE FROM findings WHERE claim_id IN (SELECT id FROM claims WHERE application_id = ?)"),
        "evidence": q("DELETE FROM evidence WHERE claim_id IN (SELECT id FROM claims WHERE application_id = ?)"),
        "claims": q("DELETE FROM claims WHERE application_id = ?"),
        "source_responses": q("DELETE FROM source_responses WHERE application_id = ?"),
        "documents": q("DELETE FROM documents WHERE application_id = ?"),
    }
    conn.commit()
    mark_application_closed(conn, app_id)
    return counts
