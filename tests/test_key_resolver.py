from app.contracts import Claim
from app.correlation.key_resolver import resolve, conflicts_for_claim
from tests.helpers import cfg

REG = cfg("document_registry.json")

def cl(cid, doc, field, raw):
    return Claim(id=cid, application_id="a", document_id=doc, field_key=field, raw_value=raw, normalized_value=raw,
                 page=1, bbox=(0, 0, 1, 1), extraction_method="pdf_text")

DT = {"form": "application_form", "deed": "land_deed", "luc": "land_use_certificate", "plan": "building_plan",
      "soc": "society_registration", "noc": "state_noc"}

def base():
    return [cl("c1", "form", "holder_name", "Green Valley Educational Society"),
            cl("c2", "soc", "holder_name", "GREEN VALLEY EDUCATIONAL SOCIETY"),
            cl("c3", "plan", "holder_name", "Green Valley Educational Society"),
            cl("c4", "deed", "holder_name", "Green Valley Educational Society"),
            cl("c5", "form", "survey_no", "45/2"), cl("c6", "deed", "survey_no", "S.No. 45/2"),
            cl("c7", "luc", "survey_no", "45 / 2"), cl("c8", "plan", "survey_no", "45/2")]

def test_all_agree():
    r = resolve(base(), DT, REG)
    assert all(kr.agrees for kr in r.values())

def test_survey_mismatch_flags_right_claims():
    cs = base(); cs[6] = cl("c7", "luc", "survey_no", "45/3")
    r = resolve(cs, DT, REG)
    assert not r["survey_no"].agrees
    assert set(r["survey_no"].disagreements[0].claim_ids) == {"c5", "c6", "c7", "c8"}
    assert conflicts_for_claim(r, "c7") and not conflicts_for_claim(r, "c1")

def test_land_owner_must_be_applicant():
    cs = base(); cs[3] = cl("c4", "deed", "holder_name", "Ramesh Kumar")
    r = resolve(cs, DT, REG)
    assert any(d.key == "land_owner_is_applicant" for d in r["land_owner"].disagreements)
    assert conflicts_for_claim(r, "c4")

def test_land_owner_matches_applicant_ok():
    assert resolve(base(), DT, REG)["land_owner"].agrees

def test_reference_intake_conflict():
    cs = [cl("c9", "noc", "approved_intake", "120")]
    r = resolve(cs, DT, REG, references={"approved_intake": ("mock_aicte", "60")})
    d = r["approved_intake"].disagreements[0]
    assert d.claim_ids == ("c9",) and "mock_aicte" in d.description and "120" in d.description

def test_reference_intake_agrees():
    r = resolve([cl("c9", "noc", "approved_intake", "60")], DT, REG, references={"approved_intake": ("mock_aicte", "60")})
    assert r["approved_intake"].agrees

def test_unreadable_not_conflict_not_agreement():
    cs = [cl("c1", "form", "built_up_area_sqm", "1000"), cl("c2", "plan", "built_up_area_sqm", "n/a")]
    kr = resolve(cs, DT, REG)["built_up_area_sqm"]
    assert kr.agrees and len(kr.unreadable) == 1

def test_area_tolerance():
    cs = [cl("c1", "form", "built_up_area_sqm", "1000"), cl("c2", "plan", "built_up_area_sqm", "1005")]
    assert resolve(cs, DT, REG)["built_up_area_sqm"].agrees
    cs[1] = cl("c2", "plan", "built_up_area_sqm", "1100")
    assert not resolve(cs, DT, REG)["built_up_area_sqm"].agrees

def test_unmapped_fields_ignored():
    assert resolve([cl("c1", "deed", "document_number", "LD-1")], DT, REG)["survey_no"].readings == ()
