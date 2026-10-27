import pytest
from app import db, ledger
from app.contracts import Claim
from app.correlation.reuse import find_reuse, seen_elsewhere_fn
from tests.helpers import cfg, make_doc

REG = cfg("document_registry.json")

@pytest.fixture
def conn():
    c = db.connect(":memory:"); db.init_db(c)
    db.create_application(c, "a1", "x", "Inst One"); db.create_application(c, "a2", "x", "Inst Two")
    return c

def add(conn, app, did, dtype, field, raw, cid):
    if db.get_document(conn, did) is None:
        import hashlib
        from app.contracts import Document
        db.insert_document(conn, Document(id=did, application_id=app, doc_type=dtype, filename=f"{did}.pdf",
                                          sha256=hashlib.sha256(did.encode()).hexdigest(), pages=1))
    ledger.record_claim(conn, Claim(id=cid, application_id=app, document_id=did, field_key=field, raw_value=raw,
                                    normalized_value=raw, page=1, bbox=(0, 0, 1, 1), extraction_method="pdf_text"))

def test_certificate_reuse(conn):
    add(conn, "a1", "d1", "fire_noc", "document_number", "FIRE/2025/77", "c1")
    add(conn, "a2", "d2", "fire_noc", "document_number", "fire 2025 77", "c2")
    f = find_reuse(conn, "a2")
    assert [(x.kind, x.claim_id, x.other_application_id) for x in f] == [("certificate", "c2", "a1")]

def test_same_number_different_doc_type_is_not_reuse(conn):
    add(conn, "a1", "d1", "fire_noc", "document_number", "123", "c1")
    add(conn, "a2", "d2", "state_noc", "document_number", "123", "c2")
    assert find_reuse(conn, "a2") == []

def test_land_reuse_and_declared_sharing(conn):
    add(conn, "a1", "d1", "land_deed", "survey_no", "45/2", "c1")
    add(conn, "a2", "d2", "land_deed", "survey_no", "S.No. 45 / 2", "c2")
    assert [x.kind for x in find_reuse(conn, "a2")] == ["land"]
    assert find_reuse(conn, "a2", declared_shared=["45/2"]) == []

def test_own_application_never_matches_itself(conn):
    add(conn, "a1", "d1", "land_deed", "survey_no", "45/2", "c1")
    assert find_reuse(conn, "a1") == []

def test_finds_reuse_in_closed_application(conn):
    add(conn, "a1", "d1", "land_deed", "survey_no", "45/2", "c1")
    db.mark_application_closed(conn, "a1")
    add(conn, "a2", "d2", "land_deed", "survey_no", "45/2", "c2")
    assert len(find_reuse(conn, "a2")) == 1

def test_seen_elsewhere_adapter(conn):
    add(conn, "a1", "d1", "land_deed", "document_number", "LD-1", "c1")
    seen = seen_elsewhere_fn(conn, "a2", REG)
    assert seen("sub_registrar", "ld 1") is True
    assert seen("sub_registrar", "LD-2") is False
    assert seen("fire_department", "LD-1") is False

def test_works_with_proof_verifier(conn):
    from datetime import date
    from app.authenticity.mock_issuer import MockIssuerNetwork
    from app.authenticity.proof_verifier import verify_proof, PrintedFacts
    from app.contracts import AuthResult
    add(conn, "a1", "d1", "land_deed", "document_number", "LD-1", "c1")
    net = MockIssuerNetwork(); p = net.issuer_for("land_deed").issue("land_deed", "LD-1", "A", {}, date(2025, 1, 1))
    r = verify_proof(p.qr_url, PrintedFacts("land_deed", "LD-1", "A"), net.trusted_issuers_config(), net.lookup,
                     seen_elsewhere=seen_elsewhere_fn(conn, "a2", REG))
    assert r.auth_result == AuthResult.AUTHENTICATED and r.flags
