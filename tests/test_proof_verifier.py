import pytest
from datetime import date
from app.authenticity.mock_issuer import MockIssuerNetwork, parse_qr
from app.authenticity.proof_verifier import verify_proof, PrintedFacts
from app.contracts import AuthResult, Strength
from tests.helpers import cfg

@pytest.fixture
def net(): return MockIssuerNetwork()

TRUSTED = None
def trusted(net): return net.trusted_issuers_config()

def issue(net, **kw):
    iss = net.issuer_for("land_deed")
    return iss, iss.issue("land_deed", "LD-1", "Asha Rao", {"survey_no": "45/2"}, date(2025, 1, 1), **kw)

def printed(**kw):
    d = dict(doc_type="land_deed", number="LD-1", holder="Asha Rao", data={"survey_no": "45/2"}); d.update(kw)
    return PrintedFacts(**d)

def test_genuine_authenticated(net):
    _, p = issue(net)
    r = verify_proof(p.qr_url, printed(), trusted(net), net.lookup)
    assert r.auth_result == AuthResult.AUTHENTICATED
    assert any(e.strength == Strength.STRONG and e.qualifies for e in r.evidence)
    assert all(c.passed for c in r.checks)

def test_absent(net):
    assert verify_proof(None, printed(), trusted(net), net.lookup).auth_result == AuthResult.PROOF_ABSENT
    assert verify_proof("  ", printed(), trusted(net), net.lookup).auth_result == AuthResult.PROOF_ABSENT

def test_malformed(net):
    assert verify_proof("https://x/verify?d=zz", printed(), trusted(net), net.lookup).auth_result == AuthResult.PROOF_INVALID

def test_forged_lookalike(net):
    f = net.forge("land_deed", "LD-1", "Asha Rao", {"survey_no": "45/2"}, date(2025, 1, 1))
    r = verify_proof(f.qr_url, printed(), trusted(net), net.lookup)
    assert r.auth_result == AuthResult.PROOF_INVALID and "look-alike" in r.problems[0]

def test_tampered_payload(net):
    import base64
    _, p = issue(net)
    dom, payload, sig = parse_qr(p.qr_url)
    payload["holder"] = "Someone Else"
    from app.authenticity.mock_issuer import canonical, _b64
    qr = f"https://{dom}/verify?d={_b64(canonical(payload))}&s={_b64(sig)}"
    assert verify_proof(qr, printed(holder="Someone Else"), trusted(net), net.lookup).auth_result == AuthResult.PROOF_INVALID

def test_copied_qr_wrong_document(net):
    _, p = issue(net)
    r = verify_proof(p.qr_url, printed(number="LD-999"), trusted(net), net.lookup)
    assert r.auth_result == AuthResult.PROOF_INVALID and "different document" in r.problems[0]

def test_survey_number_mismatch(net):
    _, p = issue(net)
    r = verify_proof(p.qr_url, printed(data={"survey_no": "99/9"}), trusted(net), net.lookup)
    assert r.auth_result == AuthResult.PROOF_INVALID and "survey_no" in r.problems[0]

def test_unreadable_printed_number(net):
    _, p = issue(net)
    assert verify_proof(p.qr_url, printed(number=None), trusted(net), net.lookup).auth_result == AuthResult.NOT_SOFTWARE_VERIFIABLE

def test_untrusted_issuer(net):
    _, p = issue(net)
    t = trusted(net); del t["issuers"]["sub_registrar"]
    assert verify_proof(p.qr_url, printed(), t, net.lookup).auth_result == AuthResult.NOT_SOFTWARE_VERIFIABLE

def test_unrecorded(net):
    _, p = issue(net, record=False)
    assert verify_proof(p.qr_url, printed(), trusted(net), net.lookup).auth_result == AuthResult.PROOF_INVALID

def test_revoked(net):
    iss, p = issue(net); iss.revoke("LD-1")
    r = verify_proof(p.qr_url, printed(), trusted(net), net.lookup)
    assert r.auth_result == AuthResult.PROOF_INVALID and "revoked" in r.problems[0]

def test_outage(net):
    _, p = issue(net); net.set_outage("sub_registrar")
    assert verify_proof(p.qr_url, printed(), trusted(net), net.lookup).auth_result == AuthResult.ISSUER_UNREACHABLE
    r = verify_proof(p.qr_url, printed(), trusted(net), net.lookup, lookup_required=False)
    assert r.auth_result == AuthResult.AUTHENTICATED and r.flags

def test_reuse_is_flag_not_failure(net):
    _, p = issue(net)
    r = verify_proof(p.qr_url, printed(), trusted(net), net.lookup, seen_elsewhere=lambda i, n: True)
    assert r.auth_result == AuthResult.AUTHENTICATED and r.flags
    assert any(e.kind == "proof_reuse" and e.strength == Strength.WEAK for e in r.evidence)

def test_shipped_trusted_issuers_matches_mock(net):
    assert cfg("trusted_issuers.json") == net.trusted_issuers_config()


AICTE_LINK = "https://approvalstatus.aicte.gov.in/api/getInstituteDetails/MS1MOTdMMDZU"

def test_unsigned_lookup_link_on_registered_domain_goes_to_human(net):
    cfg_ = dict(trusted(net)); cfg_["lookup_link_domains"] = {"approvalstatus.aicte.gov.in": "AICTE"}
    r = verify_proof(AICTE_LINK, printed(), cfg_, net.lookup)
    assert r.auth_result == AuthResult.NOT_SOFTWARE_VERIFIABLE      # not Proof invalid / Contradicted
    assert not any(e.qualifies for e in r.evidence)

def test_unsigned_link_on_unregistered_domain_still_invalid(net):
    cfg_ = dict(trusted(net)); cfg_["lookup_link_domains"] = {"approvalstatus.aicte.gov.in": "AICTE"}
    r = verify_proof("https://approvalstatus-aicte.example/api/getInstituteDetails/x", printed(), cfg_, net.lookup)
    assert r.auth_result == AuthResult.PROOF_INVALID

def test_http_lookalike_of_registered_domain_not_accepted(net):
    cfg_ = dict(trusted(net)); cfg_["lookup_link_domains"] = {"approvalstatus.aicte.gov.in": "AICTE"}
    r = verify_proof("http://approvalstatus.aicte.gov.in.evil.test/x", printed(), cfg_, net.lookup)
    assert r.auth_result == AuthResult.PROOF_INVALID

def test_ocr_spacing_inside_an_identifier_still_binds_but_a_different_number_does_not(net):
    _, p = issue(net)
    ok = verify_proof(p.qr_url, printed(number="L D-1", holder="Asha  Rao"), trusted(net), net.lookup)
    assert ok.auth_result == AuthResult.AUTHENTICATED
    bad = verify_proof(p.qr_url, printed(number="LD-2"), trusted(net), net.lookup)
    assert bad.auth_result == AuthResult.PROOF_INVALID
