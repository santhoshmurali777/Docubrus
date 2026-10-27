import pytest
from datetime import date
from pydantic import ValidationError
from app.contracts import *
from tests.helpers import make_claim, make_doc

def test_claim_needs_document_and_page():
    with pytest.raises(ValidationError): make_claim(doc="")
    with pytest.raises(ValidationError): make_claim(page=0)

def test_bbox_must_be_real_box():
    with pytest.raises(ValidationError):
        Claim(id="c", application_id="a", document_id="d", field_key="k", raw_value="v", normalized_value="v",
              page=1, bbox=(5, 5, 5, 9), extraction_method="ocr")

def test_models_frozen():
    with pytest.raises(ValidationError): make_claim().raw_value = "x"

def test_extra_fields_forbidden():
    with pytest.raises(ValidationError): Finding(id="f", claim_id="c", status=Status.INSUFFICIENT, reason="r", bogus=1)

def test_weak_never_qualifies():
    with pytest.raises(ValidationError):
        Evidence(id="e", claim_id="c", kind="k", strength=Strength.WEAK, qualifies=True, detail="d")

def test_verified_finding_needs_evidence():
    with pytest.raises(ValidationError): Finding(id="f", claim_id="c", status=Status.VERIFIED, reason="r")
    Finding(id="f", claim_id="c", status=Status.VERIFIED, reason="r", evidence_ids=("e1",))

def test_finding_needs_reason():
    with pytest.raises(ValidationError): Finding(id="f", claim_id="c", status=Status.MISSING, reason="")

def test_failed_source_needs_error():
    with pytest.raises(ValidationError): SourceResponse(id="s", source="x", ok=False)

def test_rule_maker_checker():
    kw = dict(id="r", version="1", clause_ref="c", effective_from=date(2026, 1, 1), author="a")
    with pytest.raises(ValidationError): Rule(**kw, state=RuleState.APPROVED)
    with pytest.raises(ValidationError): Rule(**kw, state=RuleState.APPROVED, approver="a")
    Rule(**kw, state=RuleState.APPROVED, approver="b")

def test_sha_length():
    with pytest.raises(ValidationError):
        Document(id="d", application_id="a", doc_type="t", filename="f", sha256="abc", pages=1)

def test_seven_statuses():
    assert len(Status) == 7
