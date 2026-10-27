"""W2: embedded digital signatures. Fixtures are signed with a throw-away root (TEST ONLY, see pades_fixture.py)."""
import os
import pytest
from datetime import datetime, timedelta, timezone

from app.authenticity import pades_fixture as pf
from app.authenticity.adapters import IssuerAdapter, MockIssuerAdapter, PadesAdapter, ProofInput, adapter_for, LiveIssuerAdapter
from app.authenticity.mock_issuer import MockIssuerNetwork
from app.authenticity.pades_verifier import PadesVerifier, TrustedRoot, load_pem_certs
from app.authenticity.proof_verifier import PrintedFacts
from app.contracts import AuthResult, Strength
from tests.helpers import ROOT

SRC = os.path.join(ROOT, "data", "synthetic", "clean", "fire_noc.pdf")
PRINTED = PrintedFacts("fire_noc", "FIRE/2025/6001", "Sri Vidya Educational Trust", {})


@pytest.fixture(scope="module")
def ca(): return pf.make_root()

@pytest.fixture(scope="module")
def pdf(): return open(SRC, "rb").read()

@pytest.fixture(scope="module")
def signed(ca, pdf): return pf.sign_pdf(pdf, ca)

def verifier(ca, doc_types=("fire_noc",), now=None):
    return PadesVerifier([TrustedRoot("fire_pades", "Test Fire Authority", doc_types, load_pem_certs(ca.root_pem()))], now)


def test_signed_pdf_authenticated(ca, signed):
    r = verifier(ca).verify(signed, PRINTED)
    assert r.auth_result == AuthResult.AUTHENTICATED
    assert [c.name for c in r.checks] == ["present", "valid", "bound", "fresh"] and all(c.passed for c in r.checks)
    assert any(e.kind == "signature_valid" and e.strength == Strength.STRONG and e.qualifies for e in r.evidence)
    # honesty: the two things we do NOT check are stated, and only as weak, non-qualifying evidence
    weak = [e for e in r.evidence if e.kind in ("revocation_unchecked", "signing_time_self_reported")]
    assert len(weak) == 2 and all(e.strength == Strength.WEAK and not e.qualifies for e in weak)
    assert any("revocation" in f for f in r.flags)


def test_unsigned_pdf_is_absent(ca, pdf):
    r = verifier(ca).verify(pdf, PRINTED)
    assert r.auth_result == AuthResult.PROOF_ABSENT and not r.evidence


def test_tampered_content_is_invalid(ca, signed):
    i = signed.find(b"stream", 200) + 12
    bad = bytearray(signed); bad[i] ^= 0x01
    r = verifier(ca).verify(bytes(bad), PRINTED)
    assert r.auth_result == AuthResult.PROOF_INVALID and not r.evidence


def test_bytes_appended_after_signing_are_invalid(ca, signed):
    r = verifier(ca).verify(pf.append_after_signing(signed), PRINTED)
    assert r.auth_result == AuthResult.PROOF_INVALID
    assert [c.name for c in r.checks if c.passed is False] == ["bound"]


def test_signer_not_on_our_list_goes_to_human(ca, signed):
    other = pf.make_root()                       # a different root: signature is sound but we do not trust it
    r = verifier(other).verify(signed, PRINTED)
    assert r.auth_result == AuthResult.NOT_SOFTWARE_VERIFIABLE and not r.evidence
    assert PadesVerifier([]).verify(signed, PRINTED).auth_result == AuthResult.NOT_SOFTWARE_VERIFIABLE


def test_root_not_registered_for_this_document_type(ca, signed):
    r = verifier(ca, doc_types=("land_deed",)).verify(signed, PRINTED)
    assert r.auth_result == AuthResult.PROOF_INVALID and "not registered" in r.problems[0]


def test_signing_time_in_future_is_invalid(ca, signed):
    past = datetime.now(timezone.utc) - timedelta(days=2)
    r = verifier(ca, now=past).verify(signed, PRINTED)
    assert r.auth_result != AuthResult.AUTHENTICATED


def test_garbage_is_not_authenticated(ca):
    assert verifier(ca).verify(b"%PDF-1.4 not really", PRINTED).auth_result in (AuthResult.PROOF_ABSENT, AuthResult.PROOF_INVALID)
    assert verifier(ca).verify(b"", PRINTED).auth_result in (AuthResult.PROOF_ABSENT, AuthResult.PROOF_INVALID)


# ---- the adapter seam -------------------------------------------------------
def test_adapters_share_one_interface(ca):
    net = MockIssuerNetwork()
    a = adapter_for("signed_qr", network=net, trusted=net.trusted_issuers_config(), pades=verifier(ca))
    b = adapter_for("pdf_signature", network=net, trusted=net.trusted_issuers_config(), pades=verifier(ca))
    assert isinstance(a, MockIssuerAdapter) and isinstance(b, PadesAdapter)
    assert isinstance(a, IssuerAdapter) and isinstance(b, IssuerAdapter) and isinstance(LiveIssuerAdapter(), IssuerAdapter)
    assert b.lookup("x").ok is False                     # no record book: says so, never invents an answer
    with pytest.raises(NotImplementedError):
        LiveIssuerAdapter().verify(ProofInput(printed=PRINTED))
    with pytest.raises(ValueError):
        adapter_for("nope", network=net, trusted={}, pades=verifier(ca))
