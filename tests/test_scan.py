"""The OCR path: scans (with real QR images) must give the same answers, degrade safely, and refuse when unreadable."""
import json
import os

import pytest

from app import db
from app.documents import ocr
from app.orchestrator import Upload, analyze
from tests.helpers import ROOT, TODAY
from tests.scan_helpers import make_scan

pytestmark = pytest.mark.skipif(not ocr.available(), reason="tesseract is not installed")
SYN = os.path.join(ROOT, "data", "synthetic")
MAN = {m["id"]: m for m in json.load(open(os.path.join(SYN, "manifest.json"), encoding="utf-8"))}


def run_scanned(sc, **kw):
    folder = os.path.join(SYN, sc)
    ups = [Upload(f, make_scan(open(os.path.join(folder, f), "rb").read(), **kw)) for f in sorted(os.listdir(folder))]
    conn = db.connect(); db.init_db(conn)
    return analyze(conn, ups, today=TODAY)


def expected(rep, sc, f):
    return MAN[sc]["expected"][f"{f['doc_type']}.{f['field_key']}"]


@pytest.mark.parametrize("sc", ["clean", "forged_qr"])
def test_scanned_copy_gives_the_same_statuses(sc):
    rep = run_scanned(sc)
    assert not rep["unrecognised_files"] and not rep["rejected_values"]
    assert [(f["doc_type"], f["field_key"]) for f in rep["findings"] if f["status"] != expected(rep, sc, f)] == []
    assert all(f["method"] == "ocr" and f["confidence"] for f in rep["findings"])


def test_noisy_tilted_scan_never_gets_a_wrong_verdict():
    rep = run_scanned("clean", noise=12, tilt=0.6)
    for f in rep["findings"]:
        assert f["status"] != "Contradicted"                       # no false conflict from an OCR slip
        if f["status"] == "Verified":
            assert expected(rep, "clean", f) == "Verified"
    assert rep["counts"]["Verified"] >= 30


def test_unreadable_scan_is_refused_not_guessed():
    rep = run_scanned("clean", blur=2.5)
    assert rep["counts"]["Verified"] == 0 and rep["counts"]["Contradicted"] == 0
    assert any(i["kind"] == "scan_quality" for i in rep["case_issues"])
