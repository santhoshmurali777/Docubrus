import itertools, re, os
import pytest
from datetime import date, timedelta
from app.contracts import AuthResult, SourceResponse, Status
from app.status import EvalInput, Conflict, evaluate, STEP_STATUS, to_finding
from tests.helpers import make_claim, strong, weak, TODAY, ROOT

def base(**kw):
    d = dict(claim=make_claim(), field_key="approved_intake", auth_result=AuthResult.AUTHENTICATED,
             quality_ok=True, today=TODAY, evidence=(strong(),))
    d.update(kw); return EvalInput(**d)

def test_clean_claim_verified():
    d = evaluate(base()); assert d.status == Status.VERIFIED and d.step == 7 and d.evidence_ids == ("e1",)

def test_no_evidence_never_verified():
    assert evaluate(base(evidence=())).status == Status.INSUFFICIENT

def test_weak_only_never_verified():
    assert evaluate(base(evidence=(weak(),))).status == Status.INSUFFICIENT

def test_missing():
    assert evaluate(base(claim=None, evidence=())).status == Status.MISSING

def test_blurry():
    assert evaluate(base(quality_ok=False)).status == Status.BLURRY

def test_expired_and_boundary():
    assert evaluate(base(valid_until=TODAY - timedelta(days=1))).status == Status.EXPIRED
    assert evaluate(base(valid_until=TODAY)).status == Status.VERIFIED

def test_conflict():
    assert evaluate(base(conflicts=(Conflict("120 vs 60"),))).status == Status.CONTRADICTED

def test_failed_or_unconsulted_source():
    assert evaluate(base(required_sources=("mock_aicte",))).status == Status.SOURCE_DOWN
    bad = SourceResponse(id="s", source="mock_aicte", ok=False, error="down")
    assert evaluate(base(required_sources=("mock_aicte",), source_responses=(bad,))).status == Status.SOURCE_DOWN
    good = SourceResponse(id="s", source="mock_aicte", ok=True)
    assert evaluate(base(required_sources=("mock_aicte",), source_responses=(good,))).status == Status.VERIFIED

@pytest.mark.parametrize("auth,exp", [
    (AuthResult.PROOF_INVALID, Status.CONTRADICTED), (AuthResult.ISSUER_UNREACHABLE, Status.SOURCE_DOWN),
    (AuthResult.PROOF_ABSENT, Status.INSUFFICIENT), (AuthResult.NOT_SOFTWARE_VERIFIABLE, Status.INSUFFICIENT)])
def test_auth_mapping(auth, exp):
    assert evaluate(base(auth_result=auth)).status == exp

def test_stray_evidence_rejected():
    with pytest.raises(ValueError): evaluate(base(evidence=(strong(cid="other"),)))

def test_deterministic():
    assert evaluate(base()) == evaluate(base())

def test_exhaustive_invariant():
    """Every combination: Verified only if every condition is clean."""
    n = 0
    for claim, auth, q, exp, conf, src, ev in itertools.product(
            (True, False), list(AuthResult), (True, False), (True, False), (True, False), ("none", "bad", "ok"),
            ("none", "weak", "strong")):
        sr = {"none": (), "bad": (SourceResponse(id="s", source="x", ok=False, error="e"),),
              "ok": (SourceResponse(id="s", source="x", ok=True),)}[src]
        inp = EvalInput(claim=make_claim() if claim else None, field_key="k", auth_result=auth, quality_ok=q, today=TODAY,
                        valid_until=TODAY - timedelta(days=1) if exp else None,
                        conflicts=(Conflict("c"),) if conf else (),
                        evidence={"none": (), "weak": (weak(),), "strong": (strong(),)}[ev],
                        required_sources=("x",) if src != "none" or True else (), source_responses=sr)
        d = evaluate(inp); n += 1
        clean = (claim and auth == AuthResult.AUTHENTICATED and q and not exp and not conf and src == "ok" and ev == "strong")
        assert (d.status == Status.VERIFIED) == bool(clean), (claim, auth, q, exp, conf, src, ev, d)
        assert d.reason
        if d.status == Status.VERIFIED: assert d.step == 7
    assert n == 2 * 5 * 2 * 2 * 2 * 3 * 3

def test_to_finding_roundtrip():
    f = to_finding(evaluate(base()), "c1", "f1"); assert f.status == Status.VERIFIED

def test_statuses_md_matches_code():
    text = open(os.path.join(ROOT, "config", "statuses.md"), encoding="utf-8").read()
    rows = re.findall(r"^\| (\d) \|.*\| \*\*(.+?)\*\* \|", text, re.M)
    assert {int(s): r for s, r in rows} == {k: v.value for k, v in STEP_STATUS.items()}
