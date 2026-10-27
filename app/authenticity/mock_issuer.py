"""
mock_issuer.py -- pretend issuing authorities for the prototype.

PURPOSE   Real issuers (registrars, fire department, local bodies...) are not
          connected. This file stands in for them so the proof verifier has
          something real to check:
            1. SIGN a document's key facts and produce a QR link (Ed25519).
            2. ANSWER record lookups ("does certificate X exist, what does it say?")
               with an outage switch and revocation.
            3. Provide a ROGUE issuer for seeding forgeries.
INPUTS    document facts (type, number, holder, key fields, dates)
OUTPUTS   IssuedProof (signed payload + QR link), SourceResponse (lookup result),
          config/trusted_issuers.json (public keys and domains only)
IMPORTS   app/contracts.py, cryptography, standard library
TESTED BY tests/test_mock_issuer.py (checks run in the build log)

THIS IS A MOCK. Keys are derived from a fixed public seed so the demo is
repeatable. They protect nothing. Domains end in .invalid, a reserved name that
can never resolve, so nothing here can be mistaken for a real authority.

A lookup has three different outcomes, and they must never be confused:
  * record found       -> ok=True,  payload {"found": True,  ...}
  * no such record     -> ok=True,  payload {"found": False}   (a real answer)
  * issuer unreachable -> ok=False, error="..."                (no answer at all)
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import parse_qs, urlparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.contracts import SourceResponse

DEMO_SEED = "saakshya-demo-seed-not-secret"

# issuer_id -> (display name, domain, document types it issues)
DEMO_ISSUERS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "registrar_societies": ("Registrar of Societies (mock)", "societies.mock.invalid",
                            ("society_registration",)),
    "sub_registrar":       ("Sub-Registrar (mock)", "registration.mock.invalid",
                            ("land_deed", "encumbrance_certificate")),
    "revenue_authority":   ("Revenue Authority (mock)", "revenue.mock.invalid",
                            ("land_use_certificate", "land_conversion_certificate")),
    "local_body":          ("Local Body / Municipality (mock)", "municipality.mock.invalid",
                            ("building_plan", "occupancy_certificate")),
    "fire_department":     ("State Fire Department (mock)", "fire.mock.invalid",
                            ("fire_noc",)),
    "state_government":    ("State Government (mock)", "state.mock.invalid",
                            ("state_noc", "affiliation_letter")),
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def canonical(obj) -> bytes:
    """One exact byte form for a payload, so signatures are reproducible."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key_from_seed(issuer_id: str) -> Ed25519PrivateKey:
    seed = hashlib.sha256(f"{DEMO_SEED}:{issuer_id}".encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_hex(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return raw.hex()


def verify_signature(public_hex: str, message: bytes, signature: bytes) -> bool:
    """True only if `signature` is a valid Ed25519 signature of `message`."""
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


# --------------------------------------------------------------------------
# What an issuer produces
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class IssuedProof:
    payload: dict            # what the issuer signed
    signature: bytes
    qr_url: str              # what the QR code on the document encodes

    @property
    def number(self) -> str:
        return self.payload["number"]


def parse_qr(qr_url: str) -> tuple[str, dict, bytes]:
    """Split a QR link into (domain, payload, signature). Raises ValueError if malformed."""
    try:
        u = urlparse(qr_url)
        q = parse_qs(u.query)
        payload = json.loads(_unb64(q["d"][0]))
        sig = _unb64(q["s"][0])
        if not u.hostname:
            raise ValueError("no domain")
        return u.hostname, payload, sig
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"malformed QR link: {e}") from e


class MockIssuer:
    """One pretend authority: signs documents and keeps its own record book."""

    def __init__(self, issuer_id: str, name: str, domain: str, doc_types: tuple[str, ...],
                 key: Optional[Ed25519PrivateKey] = None):
        self.issuer_id, self.name, self.domain, self.doc_types = issuer_id, name, domain, doc_types
        self._key = key or _key_from_seed(issuer_id)
        self._records: dict[str, dict] = {}
        self._revoked: set[str] = set()
        self.outage = False

    @property
    def public_key_hex(self) -> str:
        return public_key_hex(self._key)

    def issue(self, doc_type: str, number: str, holder: str, data: dict,
              issued_on: date, valid_until: Optional[date] = None, record: bool = True,
              claims_to_be: Optional[str] = None) -> IssuedProof:
        """Sign a document's key facts.
        record=False     -> the issuer keeps no record (seeds 'signed but unrecorded' proofs).
        claims_to_be=... -> write ANOTHER issuer's id into the payload (how a forger
                            impersonates a real authority)."""
        if doc_type not in self.doc_types:
            raise ValueError(f"{self.issuer_id} does not issue '{doc_type}'")
        payload = {"v": 1, "issuer": claims_to_be or self.issuer_id, "doc_type": doc_type, "number": number,
                   "holder": holder, "data": data, "issued_on": issued_on.isoformat(),
                   "valid_until": valid_until.isoformat() if valid_until else None}
        sig = self._key.sign(canonical(payload))
        qr = f"https://{self.domain}/verify?d={_b64(canonical(payload))}&s={_b64(sig)}"
        if record:
            self._records[number] = payload
        return IssuedProof(payload=payload, signature=sig, qr_url=qr)

    def revoke(self, number: str) -> None:
        self._revoked.add(number)

    def lookup(self, number: str) -> SourceResponse:
        """The issuer's answer to 'what do you have on this number?'"""
        src = f"mock_issuer:{self.issuer_id}"
        if self.outage:
            return SourceResponse(id=f"sr_{self.issuer_id}_{number}", source=src, ok=False,
                                  error=f"{self.name} did not respond (simulated outage)")
        rec = self._records.get(number)
        payload = ({"found": True, "revoked": number in self._revoked, **rec} if rec
                   else {"found": False, "number": number})
        return SourceResponse(id=f"sr_{self.issuer_id}_{number}", source=src, ok=True, payload=payload,
                              digest=hashlib.sha256(canonical(payload)).hexdigest())


class MockIssuerNetwork:
    """All the pretend authorities, plus a rogue one for forgeries."""

    def __init__(self):
        self.issuers = {iid: MockIssuer(iid, n, d, t) for iid, (n, d, t) in DEMO_ISSUERS.items()}
        # Not in the trusted list. Used to build look-alike forgeries.
        self.rogue = MockIssuer("rogue_issuer", "Look-alike Issuer (mock)", "societies-verify.mock.invalid",
                                tuple(t for _, _, ts in DEMO_ISSUERS.values() for t in ts))

    def issuer_for(self, doc_type: str) -> MockIssuer:
        for i in self.issuers.values():
            if doc_type in i.doc_types:
                return i
        raise ValueError(f"no mock issuer issues '{doc_type}'")

    def forge(self, doc_type: str, number: str, holder: str, data: dict,
              issued_on: date, valid_until: Optional[date] = None) -> IssuedProof:
        """A forgery of the real issuer's proof: same payload text, the real issuer's
        name, but signed with the rogue key and carrying a look-alike domain."""
        real = self.issuer_for(doc_type)
        return self.rogue.issue(doc_type, number, holder, data, issued_on, valid_until,
                                record=False, claims_to_be=real.issuer_id)

    def lookup(self, issuer_id: str, number: str) -> SourceResponse:
        if issuer_id not in self.issuers:
            return SourceResponse(id=f"sr_{issuer_id}_{number}", source=f"mock_issuer:{issuer_id}", ok=False,
                                  error=f"unknown issuer '{issuer_id}'")
        return self.issuers[issuer_id].lookup(number)

    def set_outage(self, issuer_id: str, down: bool = True) -> None:
        self.issuers[issuer_id].outage = down

    def trusted_issuers_config(self) -> dict:
        """What goes in config/trusted_issuers.json: public information only."""
        return {"_note": "MOCK issuers for the prototype. Keys are derived from a public demo seed and protect nothing.",
                "issuers": {iid: {"name": i.name, "domain": i.domain, "public_key": i.public_key_hex,
                                  "doc_types": list(i.doc_types)} for iid, i in self.issuers.items()},
                # real issuers that only publish an unsigned lookup link (no signature to verify)
                "lookup_link_domains": {"approvalstatus.aicte.gov.in": "AICTE (approval status portal)"}}

    def write_trusted_issuers(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.trusted_issuers_config(), fh, indent=2, sort_keys=True)
            fh.write("\n")
