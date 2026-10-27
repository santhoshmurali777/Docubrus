"""W2 end to end: a self-signed FIXTURE through analyze(), plus the dated AICTE snapshot."""
import copy
import dataclasses
import json
import os
from datetime import date

import pytest

from app import db
from app.aicte_snapshot import AicteSnapshot, import_csv
from app.authenticity import pades_fixture as pf
from app.authenticity.pades_verifier import PadesVerifier, TrustedRoot, load_pem_certs
from app.mock_aicte import references_for
from app.orchestrator import Upload, analyze, load_config
from tests.helpers import ROOT, TODAY

SYN = os.path.join(ROOT, "data", "synthetic")


@pytest.fixture(scope="module")
def ca(): return pf.make_root()


def make_cfg(ca, **over):
    base = load_config()
    reg = copy.deepcopy(base.registry)
    reg["document_types"]["fire_noc"]["proof"] = {"kind": "pdf_signature", "issuer_id": "fire_pades"}
    reg["document_types"]["fire_noc"]["required_sources"] = []
    pades = PadesVerifier([TrustedRoot("fire_pades", "Test Fire Authority", ("fire_noc",), load_pem_certs(ca.root_pem()))])
    return dataclasses.replace(base, registry=reg, pades=pades, **over)


def run(scenario, ca, transform=None, **kw):
    folder = os.path.join(SYN, scenario)
    ups = []
    for f in sorted(os.listdir(folder)):
        data = open(os.path.join(folder, f), "rb").read()
        if transform and f.startswith("fire_noc"):
            data = transform(data)
        ups.append(Upload(f, data))
    conn = db.connect(); db.init_db(conn)
    return analyze(conn, ups, today=TODAY, cfg=make_cfg(ca), **kw)


def fire(rep): return [f for f in rep["findings"] if f["doc_type"] == "fire_noc"]
def doc(rep): return next(d for d in rep["documents"] if d["doc_type"] == "fire_noc")


def test_genuine_signed_pdf_verifies(ca):
    rep = run("clean", ca, lambda b: pf.sign_pdf(b, ca))
    assert doc(rep)["auth_result"] == "Authenticated" and doc(rep)["issuer"] == "fire_pades"
    assert {f["status"] for f in fire(rep)} == {"Verified"}
    assert any("revocation" in x for x in doc(rep)["flags"])          # the limit is shown, not hidden


def test_unsigned_pdf_is_proof_absent_not_verified(ca):
    rep = run("clean", ca)
    assert doc(rep)["auth_result"] == "Proof absent"
    assert "Verified" not in {f["status"] for f in fire(rep)}


def test_tampered_copy_becomes_contradicted(ca):
    rep = run("clean", ca, lambda b: pf.append_after_signing(pf.sign_pdf(b, ca)))
    assert doc(rep)["auth_result"] == "Proof invalid"
    assert {f["status"] for f in fire(rep)} == {"Contradicted"}


def test_untrusted_signer_goes_to_a_human(ca):
    other = pf.make_root()
    rep = run("clean", ca, lambda b: pf.sign_pdf(b, other))
    assert doc(rep)["auth_result"] == "Not software-verifiable"
    assert "Verified" not in {f["status"] for f in fire(rep)}


def test_signed_but_expired_certificate_is_expired(ca):
    rep = run("expired_fire_noc", ca, lambda b: pf.sign_pdf(b, ca))
    assert "Expired" in {f["status"] for f in fire(rep)}


def test_default_config_is_unchanged():
    assert load_config().pades.roots == []
    assert all(s["proof"]["kind"] != "pdf_signature" for s in load_config().registry["document_types"].values())


# ---- AICTE snapshot ---------------------------------------------------------
def snap(tmp_path, d="2026-10-20", intake="60"):
    p = tmp_path / "snap.json"
    p.write_text(json.dumps({"snapshot_date": d, "origin": "test file", "institutions": {
        "SOC/2015/04401": {"approved_intake": intake, "legal_name": "Sri Vidya Educational Trust"}}}))
    return str(p)


def test_snapshot_answers_carry_their_date(tmp_path):
    s = AicteSnapshot.load(snap(tmp_path))
    r = s.lookup("soc/2015/04401")
    assert r.ok and r.payload["found"] and r.payload["snapshot_date"] == "2026-10-20"
    assert references_for(r)["approved_intake"][0] == "aicte_snapshot 2026-10-20"
    miss = s.lookup("nope")
    assert miss.ok and miss.payload["found"] is False and miss.payload["snapshot_date"] == "2026-10-20"
    assert AicteSnapshot.load(snap(tmp_path), outage=True).lookup("x").ok is False


def test_snapshot_staleness_note(tmp_path):
    s = AicteSnapshot.load(snap(tmp_path, d="2026-01-01"))
    assert "days old" in s.age_note(date(2026, 10, 26)) and s.age_note(date(2026, 2, 1)) is None


def test_snapshot_drives_the_report_and_names_its_date(tmp_path, ca):
    cfg = dataclasses.replace(make_cfg(ca), aicte_snapshot_path=snap(tmp_path, intake="60"))
    conn = db.connect(); db.init_db(conn)
    folder = os.path.join(SYN, "clean")
    rep = analyze(conn, [Upload(f, open(os.path.join(folder, f), "rb").read()) for f in sorted(os.listdir(folder))],
                  today=TODAY, cfg=cfg)
    assert any("2026-10-20" in n for n in rep["notes"])
    hit = [f for f in rep["findings"] if f["status"] == "Contradicted" and f["field_key"] == "approved_intake"]
    assert hit and all("aicte_snapshot 2026-10-20" in f["reason"] for f in hit)    # 60 in the snapshot vs 120 on the documents


def test_csv_import_needs_an_explicit_intake_mode(tmp_path):
    c = tmp_path / "a.csv"
    c.write_text("id,name,intake\nX1,Alpha,60\nX1,Alpha,120\nX2,Beta,30\n")
    out = tmp_path / "o.json"
    n = import_csv(str(c), str(out), snapshot_date=date(2026, 10, 1), origin="t", key_col="id", name_col="name",
                   intake_col="intake", intake_mode="sum")
    assert n == 2 and json.load(open(out))["institutions"]["X1"]["approved_intake"] == "180"
    with pytest.raises(ValueError):
        import_csv(str(c), str(out), snapshot_date=date(2026, 10, 1), origin="t", key_col="id", name_col="name",
                   intake_col="intake", intake_mode="guess")
    with pytest.raises(ValueError):
        import_csv(str(c), str(out), snapshot_date=date(2026, 10, 1), origin="t", key_col="nope", name_col="name",
                   intake_col="intake", intake_mode="sum")
