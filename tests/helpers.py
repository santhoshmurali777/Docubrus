"""Shared builders for tests."""
import hashlib, json, os
from datetime import date
from app.contracts import Claim, Document, Evidence, AuthResult, Strength

ROOT = os.path.join(os.path.dirname(__file__), "..")
TODAY = date(2026, 10, 26)

def cfg(name):
    with open(os.path.join(ROOT, "config", name), encoding="utf-8") as fh:
        return json.load(fh)

def make_claim(cid="c1", app="a1", doc="d1", page=1):
    return Claim(id=cid, application_id=app, document_id=doc, field_key="approved_intake",
                 raw_value="60", normalized_value="60", page=page, bbox=(0, 0, 10, 10),
                 extraction_method="pdf_text")

def make_doc(did="d1", app="a1", auth=AuthResult.AUTHENTICATED, pages=2):
    return Document(id=did, application_id=app, doc_type="land_deed", filename="deed.pdf",
                    sha256=hashlib.sha256(did.encode()).hexdigest(), pages=pages, auth_result=auth)

def strong(eid="e1", cid="c1"):
    return Evidence(id=eid, claim_id=cid, kind="issuer_record_match", strength=Strength.STRONG, qualifies=True, detail="ok")

def weak(eid="e2", cid="c1"):
    return Evidence(id=eid, claim_id=cid, kind="format_check", strength=Strength.WEAK, qualifies=False, detail="looks fine")
