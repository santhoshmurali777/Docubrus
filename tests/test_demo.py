"""Every seeded scenario must produce the statuses in data/synthetic/manifest.json (the source of EXPECTED.md)."""
import json
import os

import pytest

from app import db
from app.orchestrator import Upload, analyze
from tests.helpers import ROOT, TODAY

SYN = os.path.join(ROOT, "data", "synthetic")
MANIFEST = json.load(open(os.path.join(SYN, "manifest.json"), encoding="utf-8"))


def run(scenario_id, **kw):
    conn = db.connect(); db.init_db(conn)
    folder = os.path.join(SYN, scenario_id)
    ups = [Upload(f, open(os.path.join(folder, f), "rb").read()) for f in sorted(os.listdir(folder))]
    return analyze(conn, ups, today=TODAY, **kw)


@pytest.mark.parametrize("sc", MANIFEST, ids=[s["id"] for s in MANIFEST])
def test_scenario_matches_expected(sc):
    rep = run(sc["id"])
    assert not rep["unrecognised_files"] and not rep["rejected_values"]
    wrong = [(f["doc_type"], f["field_key"], f["status"], sc["expected"][f"{f['doc_type']}.{f['field_key']}"])
             for f in rep["findings"] if f["status"] != sc["expected"][f"{f['doc_type']}.{f['field_key']}"]]
    assert not wrong, wrong
    assert sorted(m["doc_type"] for m in rep["missing_documents"]) == sorted(sc["missing"])


def test_every_finding_is_located_and_verified_ones_cite_evidence():
    rep = run("clean")
    for f in rep["findings"]:
        assert f["page"] >= 1 and f["bbox"][2] > f["bbox"][0]
        if f["status"] == "Verified":
            assert f["evidence_ids"]


def test_issuer_outage_gives_source_unavailable():
    rep = run("clean", outages=("fire_department",))
    st = {f["status"] for f in rep["findings"] if f["doc_type"] == "fire_noc"}
    assert st == {"Source unavailable"}
    assert all(f["status"] == "Verified" for f in rep["findings"] if f["doc_type"] == "land_deed")


def test_regulator_outage_is_noted_not_failed():
    rep = run("intake_mismatch", aicte_outage=True)
    assert any(i["kind"] == "regulator_unavailable" for i in rep["case_issues"])
    assert all(f["status"] != "Contradicted" for f in rep["findings"] if f["field_key"] == "approved_intake")


def test_a_non_pdf_and_a_scan_are_reported_not_guessed():
    conn = db.connect(); db.init_db(conn)
    rep = analyze(conn, [Upload("x.pdf", b"hello"), Upload("../../etc/y.pdf", b"%PDF-1.4 broken")], today=TODAY)
    assert len(rep["unrecognised_files"]) == 2 and not rep["findings"]
    assert all("/" not in u["filename"] for u in rep["unrecognised_files"])
