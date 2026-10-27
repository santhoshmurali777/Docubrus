"""W5: reviewer actions are audited, append-only, and can never change a status."""
import sqlite3

import pytest
from pydantic import ValidationError

from app import db, ledger
from app.contracts import ReviewAction, ReviewKind, Status
from app.ledger import LedgerError
from app.orchestrator import Upload, analyze
from tests.helpers import ROOT, TODAY
from tests.test_demo import SYN
import os

FIDS = {"f1", "f2"}


def act(kind, finding="f1", note="x", actor="Asha", resolves=None, case="case1", id=None):
    return ReviewAction(id=id or ledger.new_id("act"), case_id=case, finding_id=finding, kind=ReviewKind(kind),
                        actor=actor, note=note, resolves_id=resolves)


@pytest.fixture
def conn():
    c = db.connect(); db.init_db(c)
    return c


def rec(conn, a, attention=0):
    return ledger.record_review_action(conn, a, case_finding_ids=FIDS, attention=attention)


# ---- the shape of an action --------------------------------------------------
@pytest.mark.parametrize("kw", [
    dict(kind="comment", note="  "),                       # a comment needs words
    dict(kind="clarify", note=""),
    dict(kind="attest", finding=None),                      # attestation is about one finding
    dict(kind="approve", finding="f1"),                     # approval is about the whole case
    dict(kind="resolve", resolves=None),
    dict(kind="comment", resolves="act_1"),                 # only resolve may point at another action
    dict(kind="comment", actor="   "),
])
def test_bad_shapes_are_refused(kw):
    with pytest.raises(ValidationError):
        act(**kw)


def test_actions_are_frozen():
    a = act("comment")
    with pytest.raises(ValidationError):
        a.note = "edited"


# ---- ledger rules --------------------------------------------------------------
def test_unknown_finding_is_refused(conn):
    with pytest.raises(LedgerError):
        rec(conn, act("comment", finding="not_in_this_case"))
    assert db.list_review_actions(conn, "case1") == []


def test_clarify_then_resolve_flow(conn):
    c = act("clarify", note="please send the 2025 certificate")
    rec(conn, c)
    st = ledger.review_state(conn, "case1")
    assert st["case"] == "clarification_requested" and st["open_clarifications"] == 1
    assert st["by_finding"]["f1"]["open_clarifications"] == 1
    rec(conn, act("resolve", note="", resolves=c.id))
    st = ledger.review_state(conn, "case1")
    assert st["open_clarifications"] == 0 and st["case"] == "in_review"
    with pytest.raises(LedgerError, match="already resolved"):
        rec(conn, act("resolve", note="", resolves=c.id))


def test_resolve_must_target_a_clarification_of_the_same_finding(conn):
    cm = act("comment"); rec(conn, cm)
    with pytest.raises(LedgerError):
        rec(conn, act("resolve", note="", resolves=cm.id))               # a comment is not a clarification
    with pytest.raises(LedgerError):
        rec(conn, act("resolve", note="", resolves="act_nope"))
    cl = act("clarify"); rec(conn, cl)
    with pytest.raises(LedgerError):
        rec(conn, act("resolve", finding="f2", note="", resolves=cl.id))  # different finding


def test_approve_rules(conn):
    cl = act("clarify", finding=None); rec(conn, cl)
    with pytest.raises(LedgerError, match="clarification is still open"):
        rec(conn, act("approve", finding=None, note="fine"))
    rec(conn, act("resolve", finding=None, note="", resolves=cl.id))
    with pytest.raises(LedgerError, match="written justification"):
        rec(conn, act("approve", finding=None, note=""), attention=2)       # problems remain, no reason given
    rec(conn, act("approve", finding=None, note="Institution sent the corrected certificate by post."), attention=2)
    assert ledger.review_state(conn, "case1")["case"] == "approved"
    rec(conn, act("approve", finding=None, note=""), attention=0)           # clean case: no note needed


def test_a_later_question_withdraws_approval(conn):
    rec(conn, act("approve", finding=None, note=""))
    assert ledger.review_state(conn, "case1")["case"] == "approved"
    rec(conn, act("clarify", note="one more thing"))
    assert ledger.review_state(conn, "case1")["case"] == "clarification_requested"


def test_state_new_and_attested_flag(conn):
    assert ledger.review_state(conn, "case1")["case"] == "new"
    rec(conn, act("attest", note="Checked the original at the regional office."))
    st = ledger.review_state(conn, "case1")
    assert st["case"] == "in_review" and st["by_finding"]["f1"]["attested"] is True


def test_cases_do_not_leak_into_each_other(conn):
    rec(conn, act("comment", case="case1"))
    assert ledger.review_state(conn, "case2")["actions"] == 0


def test_actions_cannot_be_edited_or_deleted(conn):
    a = act("comment"); rec(conn, a)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE review_actions SET note = 'changed' WHERE id = ?", (a.id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM review_actions WHERE id = ?", (a.id,))
    assert db.get_review_action(conn, a.id).note == "x"


# ---- the invariant: a human click never creates or changes a finding --------------
def _case(conn, scenario):
    folder = os.path.join(SYN, scenario)
    ups = [Upload(f, open(os.path.join(folder, f), "rb").read()) for f in sorted(os.listdir(folder))]
    return analyze(conn, ups, today=TODAY)


def test_review_actions_never_change_a_status(conn):
    rep = _case(conn, "intake_mismatch")
    cid = rep["case_id"]
    before = [(f.id, f.status) for f in ledger.current_findings(conn, cid)]
    counts = ledger.status_counts(conn, cid)
    ids = {f["finding_id"] for f in rep["findings"]}
    bad = next(f for f in rep["findings"] if f["status"] == "Contradicted")
    ledger.record_review_action(conn, act("attest", finding=bad["finding_id"], case=cid, note="Checked by hand: it really is 60."),
                                case_finding_ids=ids, attention=5)
    ledger.record_review_action(conn, act("approve", finding=None, case=cid, note="Accepted with the institution's letter."),
                                case_finding_ids=ids, attention=5)
    assert [(f.id, f.status) for f in ledger.current_findings(conn, cid)] == before
    assert ledger.status_counts(conn, cid) == counts
    assert ledger.status_counts(conn, cid)[Status.CONTRADICTED.value] > 0
    assert ledger.review_state(conn, cid)["case"] == "approved"
