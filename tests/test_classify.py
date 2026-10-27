import glob, os
import pytest
from fastapi.testclient import TestClient
from app.documents.classify import classify_pdf, load_signatures
from app.main import app

SYN = os.path.join(os.path.dirname(__file__), "..", "data", "synthetic")
SIGS = load_signatures()


def test_every_synthetic_registry_document_is_classified_as_its_filename():
    files = glob.glob(os.path.join(SYN, "*", "*.pdf"))
    assert files
    for f in files:
        want = os.path.basename(f)[:-4]
        got = classify_pdf(open(f, "rb").read(), SIGS)
        assert got.doc_type == want and got.verdict in ("confident", "possible"), (f, got.as_dict())


def test_not_a_pdf_is_unreadable():
    r = classify_pdf(b"hello", SIGS)
    assert r.verdict == "unreadable" and r.doc_type is None


def test_unrelated_text_is_unknown():
    import io
    from reportlab.pdfgen import canvas
    b = io.BytesIO(); c = canvas.Canvas(b); c.drawString(72, 750, "Weekly canteen menu"); c.drawString(72, 730, "Idli and sambar"); c.save()
    r = classify_pdf(b.getvalue(), SIGS)
    assert r.verdict == "unknown" and r.doc_type is None


def test_endpoint():
    f = glob.glob(os.path.join(SYN, "*", "fire_noc.pdf"))[0]
    r = TestClient(app).post("/api/classify", files=[("files", ("a.pdf", open(f, "rb"), "application/pdf"))])
    assert r.status_code == 200 and r.json()[0]["doc_type"] == "fire_noc"
