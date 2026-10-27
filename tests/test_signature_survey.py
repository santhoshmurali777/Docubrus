"""The survey tells you what is in a PDF without trusting it."""
import os

from app.authenticity import pades_fixture as pf
from app.authenticity.signature_survey import collect, main, survey_pdf
from tests.helpers import ROOT

SRC = os.path.join(ROOT, "data", "synthetic", "clean", "fire_noc.pdf")
SIGNED = os.path.join(ROOT, "data", "pades", "TEST_signed_fire_noc.pdf")


def test_unsigned_pdf_is_reported_unsigned():
    r = survey_pdf(open(SRC, "rb").read())
    assert r["signed"] is False and r["signatures"] == []


def test_signed_pdf_reports_signer_time_and_integrity():
    r = survey_pdf(open(SIGNED, "rb").read())
    s = r["signatures"][0]
    assert r["signed"] and s["intact"] and s["whole_file"] and s["signed_at"] and s["signer"] and s["issuer"]


def test_tampering_is_visible_even_though_nothing_is_trusted():
    ca = pf.make_root()
    signed = pf.sign_pdf(open(SRC, "rb").read(), ca)
    i = signed.find(b"stream", 200) + 12
    bad = bytearray(signed); bad[i] ^= 0x01             # flip one byte of signed content
    assert survey_pdf(bytes(bad))["signatures"][0]["intact"] is False
    assert survey_pdf(signed)["signatures"][0]["intact"] is True


def test_content_added_after_signing_is_flagged_as_partial_coverage():
    ca = pf.make_root()
    signed = pf.sign_pdf(open(SRC, "rb").read(), ca)
    s = survey_pdf(pf.append_after_signing(signed))["signatures"][0]
    assert s["whole_file"] is False


def test_garbage_does_not_crash():
    assert survey_pdf(b"hello")["signed"] is False
    assert survey_pdf(b"%PDF-1.4 broken")["signed"] is False


def test_cli_walks_folders_and_summarises(capsys):
    assert main([os.path.join(ROOT, "data", "pades"), os.path.join(ROOT, "data", "synthetic", "clean")]) == 0
    out = capsys.readouterr().out
    assert "SIGNED" in out and "UNSIGNED" in out and "ROOT certificate" in out
    assert len(collect([os.path.join(ROOT, "data", "pades")])) == 1
    assert main(["/nonexistent"]) == 2
