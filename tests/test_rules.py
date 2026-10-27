import os, pytest
from datetime import date
from app import db
from app.contracts import Rule, RuleState
from app.correlation.area_checks import check_area
from app.rules import rule_store, rule_engine, governance as gov
from tests.helpers import ROOT
from tests.test_area import c

@pytest.fixture
def conn():
    k = db.connect(":memory:"); db.init_db(k)
    rule_store.load_seed(k, os.path.join(ROOT, "config", "rules.json")); return k

def draft(conn, version="2", eff=date(2026, 1, 1), author="alice", minimum=3.0):
    return gov.create_draft(conn, "min_land_acres", version, "c", eff, author, {"kind": "min_value", "field": "land_area_acres", "min": minimum})

# ---- store
def test_seed_loads_once(conn):
    assert rule_store.load_seed(conn, os.path.join(ROOT, "config", "rules.json")) == 0
    assert set(rule_store.all_rule_ids(conn)) == {"area_per_seat", "min_land_acres", "intake_allowed"}

def test_approved_version_is_frozen(conn):
    r = rule_store.get(conn, "min_land_acres", "1")
    with pytest.raises(rule_store.RuleStoreError):
        rule_store.save(conn, Rule(id=r.id, version="1", clause_ref="x", effective_from=r.effective_from, author="a", definition={}))

def test_draft_replaceable_only_by_author(conn):
    draft(conn); draft(conn, minimum=4.0)
    assert rule_store.get(conn, "min_land_acres", "2").definition["min"] == 4.0
    with pytest.raises(rule_store.RuleStoreError): draft(conn, author="mallory")

def test_effective_picks_version_by_date(conn):
    r = draft(conn, eff=date(2026, 1, 1)); rep = gov.dry_run(conn, "min_land_acres", "2", [{"land_area_acres": "3.5"}])
    gov.approve(conn, "min_land_acres", "2", "bob", rep)
    assert rule_store.effective(conn, "min_land_acres", date(2025, 6, 1)).version == "1"
    assert rule_store.effective(conn, "min_land_acres", date(2026, 6, 1)).version == "2"
    assert rule_store.effective(conn, "min_land_acres", date(2020, 1, 1)) is None

def test_drafts_never_in_force(conn):
    draft(conn); assert rule_store.effective(conn, "min_land_acres", date(2026, 6, 1)).version == "1"

# ---- engine
def test_engine_kinds(conn):
    on = date(2025, 1, 1)
    assert rule_engine.evaluate(conn, "min_land_acres", on, {"land_area_acres": "3"}).passed is True
    r = rule_engine.evaluate(conn, "min_land_acres", on, {"land_area_acres": "2"}); assert r.passed is False and "illustrative" in r.reason
    assert rule_engine.evaluate(conn, "intake_allowed", on, {"approved_intake": "120"}).passed is True
    assert rule_engine.evaluate(conn, "intake_allowed", on, {"approved_intake": "100"}).passed is False

def test_engine_unjudged_is_none_never_pass(conn):
    assert rule_engine.evaluate(conn, "min_land_acres", date(2025, 1, 1), {}).passed is None
    assert rule_engine.evaluate(conn, "min_land_acres", date(2025, 1, 1), {"land_area_acres": "n/a"}).passed is None
    assert rule_engine.evaluate(conn, "min_land_acres", date(2020, 1, 1), {"land_area_acres": "9"}).passed is None
    assert rule_engine.evaluate(conn, "no_such_rule", date(2025, 1, 1), {}).passed is None

def test_evaluate_all_skips_area_rule(conn):
    ids = {r.rule_id for r in rule_engine.evaluate_all(conn, date(2025, 1, 1), {"land_area_acres": "3", "approved_intake": "60"})}
    assert ids == {"min_land_acres", "intake_allowed"}

def test_old_application_uses_old_rule(conn):
    draft(conn, eff=date(2026, 1, 1), minimum=5.0)
    gov.approve(conn, "min_land_acres", "2", "bob", gov.dry_run(conn, "min_land_acres", "2", [{"land_area_acres": "6"}]))
    assert rule_engine.evaluate(conn, "min_land_acres", date(2025, 6, 1), {"land_area_acres": "3"}).passed is True
    assert rule_engine.evaluate(conn, "min_land_acres", date(2026, 6, 1), {"land_area_acres": "3"}).passed is False

def test_area_rule_feeds_area_checks(conn):
    rule = rule_store.effective(conn, "area_per_seat", date(2025, 1, 1))
    assert [i.kind for i in check_area(c("b", "500"), c("i", "60"), None, rule)] == ["too_small"]

# ---- governance
def test_author_cannot_approve(conn):
    draft(conn); rep = gov.dry_run(conn, "min_land_acres", "2", [{"land_area_acres": "3"}])
    with pytest.raises(gov.GovernanceError): gov.approve(conn, "min_land_acres", "2", "alice", rep)

def test_approve_needs_matching_dry_run(conn):
    draft(conn); rep = gov.dry_run(conn, "min_land_acres", "2", [{"land_area_acres": "3"}])
    draft(conn, minimum=9.0)                                     # edited after the dry run
    with pytest.raises(gov.GovernanceError): gov.approve(conn, "min_land_acres", "2", "bob", rep)
    gov.approve(conn, "min_land_acres", "2", "bob", gov.dry_run(conn, "min_land_acres", "2", [{"land_area_acres": "3"}]))

def test_dry_run_report_counts(conn):
    draft(conn)
    rep = gov.dry_run(conn, "min_land_acres", "2", [{"land_area_acres": "4"}, {"land_area_acres": "1"}, {}])
    assert (rep.failures, rep.unjudged) == (1, 1)
    with pytest.raises(gov.GovernanceError): gov.dry_run(conn, "min_land_acres", "2", [])

def test_cannot_approve_twice_or_unknown(conn):
    with pytest.raises(gov.GovernanceError): gov.approve(conn, "min_land_acres", "1", "bob", gov.DryRunReport("min_land_acres", "1", "x", ()))
    with pytest.raises(gov.GovernanceError): gov.dry_run(conn, "min_land_acres", "99", [{}])

def test_approved_rule_records_both_people(conn):
    draft(conn); r = gov.approve(conn, "min_land_acres", "2", "bob", gov.dry_run(conn, "min_land_acres", "2", [{"land_area_acres": "3"}]))
    assert (r.author, r.approver, r.state) == ("alice", "bob", RuleState.APPROVED) and r.illustrative
