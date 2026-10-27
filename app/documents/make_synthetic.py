"""
make_synthetic.py -- generate seeded demo applications as PDFs.

PURPOSE   Build a clean application and several seeded fakes, so the pipeline has known
          right answers. Each scenario states what it expects; data/synthetic/EXPECTED.md
          and tests/test_demo.py are both produced from that same table.
INPUTS    config/document_registry.json, the mock issuers (deterministic demo keys)
OUTPUTS   data/synthetic/<scenario>/<doc_type>.pdf, data/synthetic/manifest.json,
          data/synthetic/EXPECTED.md, data/issuer_records.json, config/mock_aicte.json
IMPORTS   reportlab, authenticity.mock_issuer, contracts
RUN       python -m app.documents.make_synthetic
NOTE      Everything here is fictional. QR payloads are printed as text under
          "Verification link:" because this build has no QR decoder.
"""
from __future__ import annotations

import copy
import json
import os
from datetime import date
from typing import Callable, Optional

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.authenticity.mock_issuer import MockIssuerNetwork
from app.contracts import Status

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
OUT = os.path.join(ROOT, "data", "synthetic")
NAME = "Sri Vidya Educational Trust"
ADDRESS = "14 Temple Road, Sector 5, Coimbatore 641001"
CHUNK = 105


def fmt(d: date) -> str:
    return f"{d.day} {d.strftime('%B %Y')}"


# --------------------------------------------------------------------------
# One clean application, parameterised by n so numbers never collide
# --------------------------------------------------------------------------
def base_docs(n: int) -> tuple[dict, dict]:
    reg = f"SOC/2015/{4400 + n:05d}"
    survey, acres, intake, built = "45/2", "3.2", "120", "2400"
    D = {
        "society_registration": dict(number=reg, issued=date(2015, 4, 2), valid_until=None,
                                     v={"document_number": reg, "holder_name": NAME,
                                        "registration_date": fmt(date(2015, 4, 2)), "registered_address": ADDRESS}),
        "land_deed": dict(number=f"DEED/2023/{1000 + n}", issued=date(2023, 3, 10), valid_until=None,
                          v={"holder_name": NAME, "survey_no": survey, "land_area_acres": acres,
                             "registration_date": fmt(date(2023, 3, 10))}),
        "encumbrance_certificate": dict(number=f"EC/2023/{2000 + n}", issued=date(2023, 3, 20), valid_until=None,
                                        v={"holder_name": NAME, "survey_no": survey, "issue_date": fmt(date(2023, 3, 20))}),
        "land_use_certificate": dict(number=f"LUC/2023/{3000 + n}", issued=date(2023, 6, 15), valid_until=None,
                                     v={"holder_name": NAME, "survey_no": survey, "land_area_acres": acres,
                                        "permitted_use": "Educational", "issue_date": fmt(date(2023, 6, 15))}),
        "building_plan": dict(number=f"BP/2023/{4000 + n}", issued=date(2023, 9, 20), valid_until=None,
                              v={"holder_name": NAME, "survey_no": survey, "built_up_area_sqm": built,
                                 "approval_date": fmt(date(2023, 9, 20))}),
        "occupancy_certificate": dict(number=f"OC/2024/{5000 + n}", issued=date(2024, 8, 5), valid_until=None,
                                      v={"holder_name": NAME, "building_plan_number": f"BP/2023/{4000 + n}",
                                         "built_up_area_sqm": built, "issue_date": fmt(date(2024, 8, 5))}),
        "fire_noc": dict(number=f"FIRE/2025/{6000 + n}", issued=date(2025, 7, 1), valid_until=date(2027, 6, 30),
                         v={"holder_name": NAME, "issue_date": fmt(date(2025, 7, 1)),
                            "valid_until": fmt(date(2027, 6, 30)), "building_address": ADDRESS}),
        "state_noc": dict(number=f"SNOC/2025/{7000 + n}", issued=date(2025, 9, 12), valid_until=None,
                          v={"holder_name": NAME, "approved_intake": intake, "issue_date": fmt(date(2025, 9, 12))}),
    }
    for dt, d in D.items():
        d["signed"], d["forge"], d["record"] = True, False, True
        d["sign_override"] = {}                 # printed value differs from what the issuer signed
        d["v"].setdefault("document_number", d["number"])
    D["application_form"] = dict(number=None, signed=False, forge=False, record=False, sign_override={},
                                 v={"holder_name": NAME, "registration_number": reg, "survey_no": survey,
                                    "land_area_acres": acres, "built_up_area_sqm": built, "address": ADDRESS,
                                    "approved_intake": intake})
    D["financial_statement"] = dict(number="UDIN24123456AAAAAB1234", signed=False, forge=False, record=False,
                                    sign_override={},
                                    v={"holder_name": NAME, "ca_membership_no": "212345",
                                       "udin": "UDIN24123456AAAAAB1234", "financial_year": "2024-25"})
    meta = dict(reg=reg, aicte_intake="120")
    return D, meta


# --------------------------------------------------------------------------
# Scenarios. expect: (doc_type or "*", field or "*") -> Status; default is
# Verified for issuer-signed documents and Insufficient evidence for the rest.
# --------------------------------------------------------------------------
V, C, E, I = Status.VERIFIED, Status.CONTRADICTED, Status.EXPIRED, Status.INSUFFICIENT


def _forged(D, m): D["land_deed"]["forge"] = True
def _extended(D, m):
    D["fire_noc"]["sign_override"] = {"valid_until": fmt(date(2027, 6, 30))}     # what the issuer signed
    D["fire_noc"]["v"]["valid_until"] = fmt(date(2029, 6, 30))                    # what the forger printed
def _expired(D, m):
    D["fire_noc"].update(issued=date(2021, 4, 1), valid_until=date(2026, 3, 31))
    D["fire_noc"]["v"].update(issue_date=fmt(date(2021, 4, 1)), valid_until=fmt(date(2026, 3, 31)))
def _intake(D, m): m["aicte_intake"] = "60"
def _survey(D, m): D["land_use_certificate"]["v"]["survey_no"] = "46/1"
def _missing(D, m): del D["land_use_certificate"]


SCENARIOS: list[dict] = [
    dict(id="clean", title="Clean application", mutate=None, expect={},
         missing=[], what="Everything consistent, every proof valid and recorded."),
    dict(id="forged_qr", title="Forged land deed (look-alike QR)", mutate=_forged,
         expect={("land_deed", "*"): C}, missing=[],
         what="Land deed QR is signed by an untrusted key and points to a look-alike domain."),
    dict(id="extended_validity", title="Fire NOC validity extended after issue", mutate=_extended,
         expect={("fire_noc", "*"): C}, missing=[],
         what="Printed 'valid until' is 30 June 2029, but the issuer signed 30 June 2027."),
    dict(id="expired_fire_noc", title="Expired fire NOC", mutate=_expired,
         expect={("fire_noc", "*"): E}, missing=[],
         what="Genuine fire NOC whose validity ended 31 March 2026."),
    dict(id="intake_mismatch", title="Intake differs from regulator record", mutate=_intake,
         expect={("*", "approved_intake"): C}, missing=[],
         what="Documents state an intake of 120; the mock AICTE record approves 60."),
    dict(id="survey_mismatch", title="Survey number differs across documents", mutate=_survey,
         expect={("*", "survey_no"): C}, missing=[],
         what="Land use certificate cites survey 46/1; every other document says 45/2."),
    dict(id="missing_doc", title="Land use certificate not submitted", mutate=_missing,
         expect={}, missing=["land_use_certificate"],
         what="A required document is absent although later chain documents depend on it."),
]


def expected_status(doc_type: str, field: str, scenario: dict, signed: bool) -> Status:
    ex = scenario["expect"]
    for k in ((doc_type, field), (doc_type, "*"), ("*", field)):
        if k in ex:
            return ex[k]
    return V if signed else I


# --------------------------------------------------------------------------
# PDF drawing
# --------------------------------------------------------------------------
def label_for(registry: dict, doc_type: str, key: str) -> str:
    f = registry["document_types"][doc_type]["fields"][key]
    return (f.get("labels") or [f["label"]])[0]


def draw_pdf(path: str, registry: dict, doc_type: str, printed: dict, qr_url: Optional[str]) -> None:
    spec = registry["document_types"][doc_type]
    c = canvas.Canvas(path, pagesize=A4)
    c.setTitle(spec["label"])
    c.setFont("Helvetica-Bold", 15)
    c.drawString(60, 790, spec["label"])
    c.setFont("Helvetica", 9)
    c.drawString(60, 772, "SYNTHETIC DOCUMENT FOR PROTOTYPE TESTING - NOT A REAL RECORD")
    y = 730
    c.setFont("Helvetica", 11)
    for key in spec["fields"]:
        if key in printed:
            c.drawString(60, y, f"{label_for(registry, doc_type, key)}: {printed[key]}")
            y -= 24
    if qr_url:
        c.setFont("Helvetica", 8)
        c.drawString(60, 150, "Verification link (QR payload):")
        c.setFont("Courier", 6)
        yy = 138
        for i in range(0, len(qr_url), CHUNK):
            c.drawString(60, yy, qr_url[i:i + CHUNK])
            yy -= 8
    c.showPage()
    c.save()


# --------------------------------------------------------------------------
# Build everything
# --------------------------------------------------------------------------
def build(root: str = ROOT) -> dict:
    with open(os.path.join(root, "config", "document_registry.json"), encoding="utf-8") as fh:
        registry = json.load(fh)
    net = MockIssuerNetwork()
    records, aicte, manifest = [], {}, []

    for n, sc in enumerate(SCENARIOS, start=1):
        D, meta = base_docs(n)
        if sc["mutate"]:
            sc["mutate"](D, meta)
        folder = os.path.join(root, "data", "synthetic", sc["id"])
        os.makedirs(folder, exist_ok=True)
        for old in os.listdir(folder):
            os.remove(os.path.join(folder, old))
        files = []
        for dt, d in D.items():
            spec = registry["document_types"][dt]
            qr = None
            if d["signed"]:
                issuer = net.issuer_for(dt)
                data = {k: d["sign_override"].get(k, d["v"][k]) for k in spec["bind_fields"] if k in d["v"]}
                args = (dt, d["number"], d["v"]["holder_name"], data, d["issued"], d["valid_until"])
                if d["forge"]:
                    qr = net.forge(*args).qr_url
                else:
                    qr = issuer.issue(*args, record=d["record"]).qr_url
                    if d["record"]:
                        records.append(dict(doc_type=dt, number=d["number"], holder=d["v"]["holder_name"], data=data,
                                            issued_on=d["issued"].isoformat(),
                                            valid_until=d["valid_until"].isoformat() if d["valid_until"] else None))
            draw_pdf(os.path.join(folder, f"{dt}.pdf"), registry, dt, d["v"], qr)
            files.append(dt)
        aicte[meta["reg"]] = {"legal_name": NAME, "approved_intake": meta["aicte_intake"], "institution_type": "Engineering (mock)"}
        manifest.append(dict(id=sc["id"], title=sc["title"], what=sc["what"], files=files,
                             missing=sc["missing"],
                             expected={f"{dt}.{k}": expected_status(dt, k, sc, D[dt]["signed"]).value
                                       for dt in D for k in D[dt]["v"]}))

    def dump(rel, obj):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
    dump("data/issuer_records.json", {"_note": "MOCK issuer record books for the synthetic scenarios.", "records": records})
    dump("config/mock_aicte.json", {"_note": "MOCK regulator records. Not real AICTE data.", "institutions": aicte})
    dump("data/synthetic/manifest.json", manifest)
    write_expected(root)
    return {"scenarios": len(manifest), "records": len(records)}


def write_expected(root: str = ROOT) -> None:
    L = ["# Expected results for the seeded scenarios", "",
         "Generated by `python -m app.documents.make_synthetic` from the SCENARIOS table. "
         "`tests/test_demo.py` fails if the pipeline disagrees with this file.", "",
         "Default for every scenario: claims on issuer-signed documents are **Verified**; claims on the "
         "application form and financial statement are **Insufficient evidence** (self-declared / attestation "
         "check not built in this version). Only the differences are listed.", "",
         "| Scenario | What was seeded | Expected differences |", "|---|---|---|"]
    for sc in SCENARIOS:
        diffs = [f"`{k[0]}.{k[1]}` -> {v.value}" for k, v in sc["expect"].items()]
        diffs += [f"`{m}` -> Missing (listed as missing document)" for m in sc["missing"]]
        L.append(f"| `{sc['id']}` | {sc['what']} | {'; '.join(diffs) or 'none (the baseline)'} |")
    L += ["", "`*` means every field of that document type, or that field in every document.", "",
          "## Run-time options (not baked into the PDFs)", "",
          "- `outage=fire_department` on `clean`: fire NOC claims -> **Source unavailable**.",
          "- `aicte_outage=true`: intake is not compared with the regulator; a note appears under case issues.", "",
          "## Known limits", "",
          "- Proof binding covers the fields in each document type's `bind_fields`. A printed field outside that "
          "list (for example `permitted_use`) is not cross-checked against the signature.",
          "- Scans are read with English-only OCR and a page quality gate. Pages that fail it give "
          "**Blurry scan**, and a scan whose identifying fields were read with low confidence is never "
          "called Contradicted. Tested on rasterised copies of these PDFs (`tests/test_scan.py`), not on real scans.",
          "- Text PDFs: the QR link is read from the printed text, falling back to a QR image. Scans: the QR image only.",
          "- Rules are illustrative until checked against the official Handbook."]
    with open(os.path.join(root, "data", "synthetic", "EXPECTED.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    print(build())
