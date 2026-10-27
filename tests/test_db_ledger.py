import pytest
from app import db, ledger
from app.contracts import *
from tests.helpers import make_claim, make_doc, strong, weak

@pytest.fixture
def conn():
    c = db.connect(":memory:"); db.init_db(c)
    db.create_application(c, "a1", "new_institute", "Test Inst")
    return c

def seed(conn, auth=AuthResult.AUTHENTICATED):
    db.insert_document(conn, make_doc(auth=auth)); ledger.record_claim(conn, make_claim())

def test_roundtrip(conn):
    seed(conn); assert db.get_claim(conn, "c1").raw_value == "60"
    assert db.get_document(conn, "d1").auth_result == AuthResult.AUTHENTICATED

def test_claim_needs_real_document_and_page(conn):
    db.insert_document(conn, make_doc())
    with pytest.raises(ledger.LedgerError): ledger.record_claim(conn, make_claim(doc="nope"))
    with pytest.raises(ledger.LedgerError): ledger.record_claim(conn, make_claim(page=3))

def test_verified_requires_authenticated_doc(conn):
    seed(conn, AuthResult.PROOF_ABSENT); ledger.record_evidence(conn, strong())
    with pytest.raises(ledger.LedgerError):
        ledger.record_finding(conn, Finding(id="f1", claim_id="c1", status=Status.VERIFIED, reason="r", evidence_ids=("e1",)))

def test_verified_requires_strong_qualifying(conn):
    seed(conn); ledger.record_evidence(conn, weak())
    with pytest.raises(ledger.LedgerError):
        ledger.record_finding(conn, Finding(id="f1", claim_id="c1", status=Status.VERIFIED, reason="r", evidence_ids=("e2",)))

def test_verified_ok_and_explain(conn):
    seed(conn); ledger.record_evidence(conn, strong())
    ledger.record_finding(conn, Finding(id="f1", claim_id="c1", status=Status.VERIFIED, reason="r", evidence_ids=("e1",)))
    assert ledger.status_counts(conn, "a1")["Verified"] == 1
    assert "deed.pdf, page 1" in ledger.explain(conn, "f1")

def test_stray_evidence_rejected(conn):
    seed(conn)
    with pytest.raises(ledger.LedgerError):
        ledger.record_finding(conn, Finding(id="f1", claim_id="c1", status=Status.INSUFFICIENT, reason="r", evidence_ids=("zzz",)))

def test_history_keeps_older_findings(conn):
    seed(conn)
    ledger.record_finding(conn, Finding(id="f1", claim_id="c1", status=Status.INSUFFICIENT, reason="r"))
    ledger.record_finding(conn, Finding(id="f2", claim_id="c1", status=Status.EXPIRED, reason="r"))
    assert [f.id for f in ledger.finding_history(conn, "c1")] == ["f1", "f2"]
    assert ledger.latest_finding(conn, "c1").id == "f2"

def test_closed_application_refuses_writes(conn):
    seed(conn); db.mark_application_closed(conn, "a1")
    with pytest.raises(ledger.LedgerError): ledger.record_evidence(conn, strong())

def test_records_cannot_be_updated_in_place(conn):
    seed(conn); ledger.record_evidence(conn, strong())
    import sqlite3
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE evidence SET detail='x' WHERE id='e1'")
