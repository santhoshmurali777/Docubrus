from datetime import date
from app.contracts import Claim, Rule
from app.correlation.area_checks import check_area, area_conflicts
from tests.test_chain import cl

RULE = Rule(id="area_per_seat", version="1", clause_ref="illustrative", effective_from=date(2024, 1, 1), author="x",
            definition={"min_built_up_sqm_per_intake_seat": 10.0, "plan_tolerance_rel": 0.01})
def c(cid, raw): return cl(cid, "d", "built_up_area_sqm", raw)

def test_ok():
    assert check_area(c("b", "700"), c("i", "60"), c("p", "700"), RULE) == []

def test_too_small_flags_both_claims():
    iss = check_area(c("b", "500"), c("i", "60"), None, RULE)
    assert [i.kind for i in iss] == ["too_small"] and set(iss[0].claim_ids) == {"b", "i"} and iss[0].rule_version == "1"
    assert area_conflicts(iss, "b")

def test_exactly_at_norm_passes():
    assert check_area(c("b", "600"), c("i", "60"), None, RULE) == []

def test_exceeds_plan_with_tolerance():
    assert check_area(c("b", "704"), c("i", "60"), c("p", "700"), RULE) == []
    assert [i.kind for i in check_area(c("b", "720"), c("i", "60"), c("p", "700"), RULE)] == ["exceeds_plan"]

def test_no_rule_is_not_a_pass():
    iss = check_area(c("b", "700"), c("i", "60"), None, None)
    assert iss[0].kind == "no_rule" and not area_conflicts(iss, "b")

def test_unreadable_not_a_conflict():
    iss = check_area(c("b", "n/a"), c("i", "60"), None, RULE)
    assert iss[0].kind == "unreadable" and not area_conflicts(iss, "b")
    iss = check_area(c("b", "700"), c("i", "sixty"), None, RULE)
    assert iss[0].kind == "unreadable"

def test_missing_built_up():
    assert check_area(None, c("i", "60"), None, RULE)[0].kind == "unreadable"
