"""
pades_fixture.py -- TEST FIXTURES ONLY: a throw-away certificate authority and a signer that
digitally signs a PDF (PAdES-style, via pyHanko).

PURPOSE   Real government-signed PDFs are hard to obtain. To show that the verifier works end to end we
          sign a PDF ourselves with a self-made root and check it with the same code that would check a
          real one. These keys protect nothing. Anything signed here is a FIXTURE, not a real document,
          and reports must say so.
OUTPUTS   make_root() -> TestCA; sign_pdf(); tamper_*() helpers; python -m app.authenticity.pades_fixture
          writes data/pades/ (root PEM, signed PDF, tampered copies).
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from pyhanko.keys import load_cert_from_pemder  # noqa: F401  (kept for callers that load PEM roots)
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.sign import fields, signers
from pyhanko_certvalidator.registry import SimpleCertificateStore  # noqa: F401
from asn1crypto import x509 as a1x509, keys as a1keys


def _name(cn: str, org: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, org), x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject, issuer, pub, signer_key, *, ca: bool, nbf, nae, serial=None):
    b = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(pub)
         .serial_number(serial or x509.random_serial_number()).not_valid_before(nbf).not_valid_after(nae)
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if ca:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                                          encipher_only=False, decipher_only=False), critical=True)
    else:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_cert_sign=False, crl_sign=False,
                                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                                          encipher_only=False, decipher_only=False), critical=True)
    return b.sign(signer_key, hashes.SHA256())


@dataclass
class TestCA:
    __test__ = False
    root_cert: x509.Certificate
    signer_cert: x509.Certificate
    signer_key: rsa.RSAPrivateKey

    def root_pem(self) -> bytes:
        return self.root_cert.public_bytes(serialization.Encoding.PEM)


def make_root(org: str = "Saakshya TEST Authority (fixture)", signer_cn: str = "Test Fire Services Signer (fixture)",
              *, now: datetime | None = None, signer_valid_days: int = 365) -> TestCA:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = _name(f"{org} Root", org)
    root = _cert(ca_name, ca_name, ca_key.public_key(), ca_key, ca=True, nbf=now - timedelta(days=30), nae=now + timedelta(days=3650))
    sk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = _cert(_name(signer_cn, org), ca_name, sk.public_key(), ca_key, ca=False,
                   nbf=now - timedelta(days=7), nae=now + timedelta(days=signer_valid_days))
    return TestCA(root, signer, sk)


def _to_signer(ca: TestCA) -> signers.SimpleSigner:
    from pyhanko_certvalidator.registry import SimpleCertificateStore
    store = SimpleCertificateStore()
    store.register(a1x509.Certificate.load(ca.root_cert.public_bytes(serialization.Encoding.DER)))
    key = a1keys.PrivateKeyInfo.load(ca.signer_key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    cert = a1x509.Certificate.load(ca.signer_cert.public_bytes(serialization.Encoding.DER))
    return signers.SimpleSigner(signing_cert=cert, signing_key=key, cert_registry=store)


def sign_pdf(pdf_bytes: bytes, ca: TestCA, *, field_name: str = "Signature1", reason: str = "TEST FIXTURE signature") -> bytes:
    w = IncrementalPdfFileWriter(io.BytesIO(pdf_bytes))
    fields.append_signature_field(w, fields.SigFieldSpec(sig_field_name=field_name))
    meta = signers.PdfSignatureMetadata(field_name=field_name, reason=reason)
    out = signers.sign_pdf(w, meta, signer=_to_signer(ca))
    return out.getvalue() if hasattr(out, "getvalue") else bytes(out.read())


def tamper_bytes(signed: bytes, old: bytes, new: bytes) -> bytes:
    """Change printed content inside the signed byte range (same length, so offsets and the signature stay put)."""
    if len(old) != len(new) or old not in signed:
        raise ValueError("old must occur in the PDF and new must have the same length")
    return signed.replace(old, new, 1)


def append_after_signing(signed: bytes, extra: bytes = b"\n% appended after signing\n") -> bytes:
    """Add bytes after the signature (an unsigned incremental update): the signature no longer covers the whole file."""
    return signed + extra


def main(out_dir: str | None = None, src: str | None = None) -> None:
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    out_dir = out_dir or os.path.join(root, "data", "pades")
    src = src or os.path.join(root, "data", "synthetic", "clean", "fire_noc.pdf")
    os.makedirs(out_dir, exist_ok=True)
    ca = make_root()
    signed = sign_pdf(open(src, "rb").read(), ca)
    open(os.path.join(out_dir, "TEST_root.pem"), "wb").write(ca.root_pem())
    open(os.path.join(out_dir, "TEST_signed_fire_noc.pdf"), "wb").write(signed)
    print(f"wrote fixtures to {out_dir} (self-made root; NOT real documents)")


if __name__ == "__main__":
    main()
