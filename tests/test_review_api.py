"""W5 over HTTP: triage list, page images, highlight boxes, reviewer actions."""
import io
import os

import numpy as np

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.main as main
from app.documents import ocr
from tests.helpers import ROOT
from tests.scan_helpers import make_scan
from tests.test_demo import SYN


@pytest.fixture
def client():
    main._conn = None; main._reports.clear(); main._files.clear()
    return TestClient(main.app)


def demo(c, sc):
    r = c.post(f"/api/demo/{sc}")
    assert r.status_code == 200, r.text
    return r.json()


# ---- triage ---------------------------------------------------------------------
def test_cases_are_listed_worst_first(client):
    clean, bad = demo(client, "clean"), demo(client, "intake_mismatch")
    rows = client.get("/api/cases").json()
    assert [r["case_id"] for r in rows][0] == bad["case_id"]
    assert [r["risk"] for r in rows] == sorted((r["risk"] for r in rows), reverse=True)
    top = rows[0]
    assert top["institution"] and top["attention"] >= 1 and top["review_state"] == "new" and top["documents"] == len(bad["documents"])
    assert {r["case_id"] for r in rows} == {clean["case_id"], bad["case_id"]}


def test_missing_documents_raise_the_risk(client):
    demo(client, "missing_doc"); demo(client, "clean")
    rows = {r["institution"]: r for r in client.get("/api/cases").json()}
    assert any(r["missing"] >= 1 for r in rows.values())


# ---- page images and highlight boxes -----------------------------------------------
def _png(client, rep, doc, page):
    r = client.get(f"/api/cases/{rep['case_id']}/documents/{doc['id']}/pages/{page}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    return Image.open(io.BytesIO(r.content)).convert("L")


def _ink_under_boxes(client, rep, shift=0.0):
    """Every finding's box must sit on printed ink, i.e. the highlight lands where the value really is."""
    docs = {d["id"]: d for d in rep["documents"]}
    for f in rep["findings"]:
        d = docs[f["document_id"]]
        ps = next(p for p in d["page_sizes"] if p["page"] == f["page"])
        x0, y0, x1, y1 = f["bbox"]
        y0, y1 = y0 + shift, y1 + shift
        assert -1 <= x0 < x1 <= ps["width"] + 1 and -1 <= y0 < y1 <= ps["height"] + 1, f
        img = _png(client, rep, d, f["page"])
        sx, sy = img.width / ps["width"], img.height / ps["height"]
        assert abs(sx - sy) / sx < 0.01                                  # same proportions as the boxes
        crop = img.crop((int(x0 * sx), int(y0 * sy), int(x1 * sx) + 1, int(y1 * sy) + 1))
        dark = float((np.asarray(crop) < 140).mean())
        assert dark > 0.02, (f["doc_type"], f["field_key"], round(dark, 4))


def test_boxes_land_on_ink_in_text_pdfs(client):
    _ink_under_boxes(client, demo(client, "clean"))


@pytest.mark.skipif(not ocr.available(), reason="tesseract is not installed")
def test_boxes_land_on_ink_in_scans(client):
    folder = os.path.join(SYN, "clean")
    files = [("files", (f, make_scan(open(os.path.join(folder, f), "rb").read()), "application/pdf"))
             for f in sorted(os.listdir(folder))]
    r = client.post("/api/cases?institution_name=Scan%20Trust", files=files)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert all(f["method"] == "ocr" for f in rep["findings"])
    _ink_under_boxes(client, rep)


def test_page_endpoint_refuses_bad_requests(client):
    rep = demo(client, "clean"); d = rep["documents"][0]; base = f"/api/cases/{rep['case_id']}/documents"
    assert client.get(f"{base}/{d['id']}/pages/0").status_code == 404
    assert client.get(f"{base}/{d['id']}/pages/{d['pages'] + 1}").status_code == 404
    assert client.get(f"{base}/doc_nope/pages/1").status_code == 404
    assert client.get(f"{base}/../../etc/passwd/pages/1").status_code == 404
    assert client.get("/api/cases/case_nope/documents/x/pages/1").status_code == 404
    assert client.get(f"{base}/{d['id']}/pages/abc").status_code == 422


def test_page_is_404_when_the_original_is_no_longer_held(client):
    rep = demo(client, "clean"); main._files.clear()
    assert client.get(f"/api/cases/{rep['case_id']}/documents/{rep['documents'][0]['id']}/pages/1").status_code == 404
    assert client.get(f"/api/cases/{rep['case_id']}").status_code == 200          # the report itself survives


def test_stored_pdfs_are_bounded(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_STORED_BYTES", 1)
    a = demo(client, "clean"); b = demo(client, "intake_mismatch")
    assert a["case_id"] not in main._files and b["case_id"] in main._files          # oldest dropped, newest kept


# ---- reviewer actions -----------------------------------------------------------------
def post(client, rep, **body):
    return client.post(f"/api/cases/{rep['case_id']}/actions", json=body)


def test_comment_clarify_resolve_approve_roundtrip(client):
    rep = demo(client, "intake_mismatch"); fid = rep["findings"][0]["finding_id"]
    r = post(client, rep, kind="clarify", actor="Asha", finding_id=fid, note="Send the AICTE letter.")
    assert r.status_code == 200 and r.json()["state"]["case"] == "clarification_requested"
    cl = r.json()["action"]["id"]
    assert post(client, rep, kind="approve", actor="Asha", note="ok").status_code == 409        # a question is open
    assert post(client, rep, kind="resolve", actor="Asha", finding_id=fid, resolves_id=cl).status_code == 200
    r = post(client, rep, kind="approve", actor="Asha", note="Letter received and matches.")
    assert r.status_code == 200 and r.json()["state"]["case"] == "approved"
    got = client.get(f"/api/cases/{rep['case_id']}/review").json()
    assert [a["kind"] for a in got["actions"]] == ["clarify", "resolve", "approve"] and got["actions"][0]["actor"] == "Asha"
    assert client.get("/api/cases").json()[0]["review_state"] == "approved"


def test_action_errors_are_plain_messages(client):
    rep = demo(client, "intake_mismatch"); fid = rep["findings"][0]["finding_id"]
    for body, code in [
        (dict(kind="comment", actor="", note="x"), 400),                                           # no name
        (dict(kind="comment", actor="A", note=""), 400),                                           # no words
        (dict(kind="attest", actor="A", note="x"), 400),                                           # attest needs a finding
        (dict(kind="comment", actor="A", note="x", finding_id="fnd_other_case"), 409),
        (dict(kind="approve", actor="A", note=""), 409),                                           # problems remain, no reason
        (dict(kind="delete", actor="A", note="x"), 422),
        (dict(kind="comment", actor="A", note="x" * 2001), 422),
    ]:
        r = post(client, rep, **body)
        assert r.status_code == code, (body, r.status_code, r.text)
        assert "detail" in r.json()
    assert client.get(f"/api/cases/{rep['case_id']}/review").json()["actions"] == []              # nothing half-written
    assert post(client, dict(case_id="case_nope"), kind="comment", actor="A", note="x").status_code == 404


def test_actions_do_not_change_the_report(client):
    rep = demo(client, "intake_mismatch")
    bad = next(f for f in rep["findings"] if f["status"] == "Contradicted")
    assert post(client, rep, kind="attest", actor="A", finding_id=bad["finding_id"], note="checked").status_code == 200
    assert client.get(f"/api/cases/{rep['case_id']}").json()["findings"] == rep["findings"]
    assert client.get(f"/api/cases/{rep['case_id']}").json()["counts"] == rep["counts"]


def test_reviewer_text_is_returned_as_data_not_markup(client):
    rep = demo(client, "clean"); fid = rep["findings"][0]["finding_id"]
    evil = "<img src=x onerror=alert(1)>"
    r = post(client, rep, kind="comment", actor=evil, finding_id=fid, note=evil)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json()["action"]["note"] == evil


def test_ink_check_is_not_vacuous(client):
    """Move every box 60pt down: the same check must now fail, so a passing run really means 'aligned'."""
    with pytest.raises(AssertionError):
        _ink_under_boxes(client, demo(client, "clean"), shift=60)
