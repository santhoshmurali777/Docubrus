from app.contracts import Claim
from app.correlation.chain_checks import check_chain, issues_for_claim
from tests.helpers import cfg

REG = cfg("document_registry.json")
def cl(cid, doc, field, raw):
    return Claim(id=cid, application_id="a", document_id=doc, field_key=field, raw_value=raw, normalized_value=raw,
                 page=1, bbox=(0, 0, 1, 1), extraction_method="pdf_text")
DT = {"deed": "land_deed", "luc": "land_use_certificate", "plan": "building_plan", "occ": "occupancy_certificate"}
def full():
    return [cl("a", "deed", "registration_date", "10/01/2024"), cl("b", "luc", "issue_date", "15 March 2024"),
            cl("c", "plan", "approval_date", "2024-06-01"), cl("d", "occ", "issue_date", "01/09/2026")]

def test_good_chain():
    assert check_chain(full(), DT, REG) == []

def test_equal_dates_allowed():
    cs = full(); cs[1] = cl("b", "luc", "issue_date", "10/01/2024")
    assert check_chain(cs, DT, REG) == []

def test_out_of_order():
    cs = full(); cs[2] = cl("c", "plan", "approval_date", "01/02/2024")
    iss = check_chain(cs, DT, REG)
    assert [i.kind for i in iss] == ["order"] and iss[0].claim_ids == ("b", "c")
    assert issues_for_claim(iss, "c") and not issues_for_claim(iss, "a")

def test_missing_middle_document():
    dt = {k: v for k, v in DT.items() if k != "luc"}
    iss = check_chain([c for c in full() if c.id != "b"], dt, REG)
    assert [(i.kind, i.doc_type) for i in iss] == [("missing", "land_use_certificate")]

def test_nothing_missing_when_chain_not_started_later():
    dt = {"deed": "land_deed"}
    assert check_chain([full()[0]], dt, REG) == []

def test_unreadable_date_reported():
    cs = full(); cs[1] = cl("b", "luc", "issue_date", "sometime in March")
    assert [i.kind for i in check_chain(cs, DT, REG)] == ["unreadable"]

def test_order_skips_over_missing_doc():
    dt = {k: v for k, v in DT.items() if k != "luc"}
    cs = [c for c in full() if c.id != "b"]; cs[1] = cl("c", "plan", "approval_date", "01/01/2023")
    kinds = sorted(i.kind for i in check_chain(cs, dt, REG)); assert kinds == ["missing", "order"]
