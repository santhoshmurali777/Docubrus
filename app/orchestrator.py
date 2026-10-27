"""
orchestrator.py -- run one application through the whole pipeline.

PURPOSE   PDFs in, findings out. For each upload: read text, detect the document type,
          extract and ground claims, authenticate the document's proof. Then run the
          cross-document checks (shared keys, regulator record, approval chain, area and
          rules) and give every claim a status through status.evaluate (the single
          place statuses are decided).
INPUTS    connection, list[Upload], options (date, simulated outages)
OUTPUTS   a JSON-ready report dict: documents, findings, missing documents, case issues
IMPORTS   everything under app/ that the build has so far
NOTE      Scans go through OCR and the page quality gate. Not built, and reported rather than
          faked: attestation registry lookups, consent, forensics, non-English OCR.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Optional

from app import db, ledger
from app.authenticity.mock_issuer import MockIssuerNetwork
from app.authenticity.adapters import ProofInput, adapter_for
from app.authenticity.pades_verifier import PadesVerifier
from app.authenticity.proof_verifier import EvidenceSpec, PrintedFacts
from app.contracts import AuthResult, Document, Evidence, Strength, utcnow
from app.correlation import chain_checks, key_resolver, reuse
from app.correlation.area_checks import area_conflicts, check_area
from app.documents import ocr, qr
from app.documents.extract import detect_doc_type, extract_fields, extract_qr_url
from app.documents.grounding import ground, ground_ocr
from app.documents.pdf_text import read_pdf
from app.ledger import new_id
from app.aicte_snapshot import AicteSnapshot
from app.correlation.normalizers import normalize_date
from app.mock_aicte import MockAicte, load_records, references_for
from app.rules import rule_engine, rule_store
from app.status import Conflict, EvalInput, evaluate, to_finding

ROOT = os.path.join(os.path.dirname(__file__), "..")
DEFAULT_CATEGORY = "new_engineering_institute"
MAX_NAME = 120
BOUND_CONF = 85.0      # a scan whose identifying fields were read below this is 'Blurry', never 'Contradicted'


@dataclass(frozen=True)
class Upload:
    filename: str
    data: bytes


@dataclass(frozen=True)
class Config:
    registry: dict
    trusted: dict
    issuer_records: list
    aicte: dict
    rules_path: str
    pades: Optional[PadesVerifier] = None          # trusted roots for embedded PDF signatures (may be empty)
    aicte_snapshot_path: Optional[str] = None      # set SAAKSHYA_AICTE_SNAPSHOT to use an imported snapshot instead of the mock


@lru_cache(maxsize=1)
def load_config(root: str = ROOT) -> Config:
    def j(*p):
        with open(os.path.join(root, *p), encoding="utf-8") as fh:
            return json.load(fh)
    recs_path = os.path.join(root, "data", "issuer_records.json")
    records = j("data", "issuer_records.json")["records"] if os.path.exists(recs_path) else []
    return Config(j("config", "document_registry.json"), j("config", "trusted_issuers.json"), records,
                  load_records(os.path.join(root, "config", "mock_aicte.json")),
                  os.path.join(root, "config", "rules.json"),
                  PadesVerifier.from_config(j("config", "pades_roots.json"), root),
                  os.environ.get("SAAKSHYA_AICTE_SNAPSHOT") or None)


def build_network(cfg: Config, outages=()) -> MockIssuerNetwork:
    net = MockIssuerNetwork()
    for r in cfg.issuer_records:
        net.issuer_for(r["doc_type"]).issue(
            r["doc_type"], r["number"], r["holder"], r["data"], date.fromisoformat(r["issued_on"]),
            date.fromisoformat(r["valid_until"]) if r["valid_until"] else None, record=True)
    for o in outages:
        if o not in net.issuers:
            raise ValueError(f"unknown issuer '{o}'")
        net.set_outage(o)
    return net


def _find_qr(d: dict) -> Optional[str]:
    """Scans: decode the QR image (OCR'd URL text is never trusted). Text PDFs: printed link first, then any QR image."""
    if d["scan"]:
        return qr.decode_images(d["images"])
    printed = extract_qr_url(d["pdf"])
    if printed:
        return printed
    try:
        return qr.decode_images(ocr.render_pages(d["data"]))
    except Exception:
        return None


def _quality_ok(d: dict, c) -> bool:
    if not d["scan"]:
        return True
    q = d["quality"].get(c.page)
    return bool(q and q.ok and (c.ocr_confidence or 0) >= ocr.CONF_MIN and not d["unreliable"])


def _data_note(cfg: Config) -> str:
    reg = "The regulator record is MOCK data."
    if cfg.aicte_snapshot_path:
        try:
            reg = f"The regulator record is an imported snapshot dated {AicteSnapshot.load(cfg.aicte_snapshot_path).snapshot_date.isoformat()}, not a live lookup."
        except Exception:
            reg = "The regulator snapshot could not be read."
    roots = len(cfg.pades.roots) if cfg.pades else 0
    return f"Issuers other than registered PDF-signature roots are MOCK. {reg}" + (f" {roots} PDF-signature root(s) are configured." if roots else "")


def _clean_name(name: str) -> str:
    return (os.path.basename(name or "upload.pdf") or "upload.pdf")[:MAX_NAME]


# --------------------------------------------------------------------------
def analyze(conn, uploads: list[Upload], *, category: str = DEFAULT_CATEGORY, institution_name: str = "(unnamed)",
            today: Optional[date] = None, outages=(), aicte_outage: bool = False,
            cfg: Optional[Config] = None) -> dict:
    cfg = cfg or load_config()
    today = today or date.today()
    reg = cfg.registry
    if category not in reg["categories"]:
        raise ValueError(f"unknown category '{category}'")
    rule_store.load_seed(conn, cfg.rules_path)
    case_id = new_id("case")
    db.create_application(conn, case_id, category, institution_name[:MAX_NAME])
    net = build_network(cfg, outages)

    unrecognised: list[dict] = []
    rejected: list[dict] = []
    docs: list[dict] = []
    issues_pre: list[dict] = []

    # ---- 1. read, detect, extract, ground ---------------------------------
    for up in uploads:
        name = _clean_name(up.filename)
        try:
            pdf = read_pdf(up.data)
        except ValueError as e:
            unrecognised.append({"filename": name, "reason": str(e)}); continue
        scan, quality, images = False, {}, []
        if not pdf.has_text_layer:
            if not ocr.available():
                unrecognised.append({"filename": name, "reason": "this is a scan and the OCR engine (tesseract) is not installed, so nothing was read"}); continue
            try:
                res = ocr.ocr_pdf(up.data)
            except Exception as e:
                unrecognised.append({"filename": name, "reason": f"scan could not be read: {e}"}); continue
            pdf, quality, images, scan = res.pdf, res.quality, res.images, True
            if not pdf.has_text_layer:
                unrecognised.append({"filename": name, "reason": "nothing legible was found, even with OCR"}); continue
        dt = detect_doc_type(pdf, reg, fuzzy=scan)
        if dt is None:
            unrecognised.append({"filename": name, "reason": "document type not recognised from its heading"}); continue
        spec = reg["document_types"][dt]
        doc = Document(id=new_id("doc"), application_id=case_id, doc_type=dt, filename=name,
                       sha256=pdf.sha256, pages=len(pdf.pages))
        db.insert_document(conn, doc)
        ftypes = {k: f["type"] for k, f in spec["fields"].items()}
        props = extract_fields(pdf, dt, reg)
        claims, rej = (ground_ocr(pdf, props, case_id, doc.id, ftypes) if scan
                       else ground(up.data, props, case_id, doc.id, ftypes))
        for c in claims:
            ledger.record_claim(conn, c)
        rejected += [{"filename": name, "field": r.field_key, "value": r.raw_value, "reason": r.reason} for r in rej]
        bound = {spec.get("number_field"), spec.get("holder_field"), *spec["bind_fields"]}
        unreliable = scan and any(c.field_key in bound and (c.ocr_confidence or 0) < BOUND_CONF for c in claims)
        for q in quality.values():
            if not q.ok:
                issues_pre.append({"kind": "scan_quality", "text": f"{name}, page {q.page}: {q.reason}; ask for a clearer scan"})
        docs.append({"doc": doc, "spec": spec, "pdf": pdf, "claims": claims, "scan": scan, "quality": quality,
                     "images": images, "data": up.data, "unreliable": unreliable})

    # ---- 2. authenticate each document ------------------------------------
    seen = reuse.seen_elsewhere_fn(conn, case_id, reg)
    for d in docs:
        spec, doc = d["spec"], d["doc"]
        by_key = {c.field_key: c for c in d["claims"]}
        kind = spec["proof"]["kind"]
        d.update(valid_until=None, checks=[], problems=[], flags=[], issuer=None, responses=[],
                 required_sources=list(spec["required_sources"]))
        sr_map: dict[str, str] = {}
        if kind in ("signed_qr", "pdf_signature"):
            nf, hf = spec.get("number_field"), spec.get("holder_field")
            printed = PrintedFacts(doc.doc_type, by_key[nf].raw_value if nf in by_key else None,
                                   by_key[hf].raw_value if hf in by_key else None,
                                   {k: by_key[k].raw_value for k in spec["bind_fields"] if k in by_key})
            adapter = adapter_for(kind, network=net, trusted=cfg.trusted, pades=cfg.pades or PadesVerifier([]), seen=seen,
                                  lookup_required=spec["proof"].get("lookup_required", True))
            res = adapter.verify(ProofInput(printed=printed, pdf_bytes=d["data"],
                                            qr_url=_find_qr(d) if kind == "signed_qr" else None))
            if kind == "pdf_signature" and res.valid_until is None and spec.get("validity_field") in by_key:
                res = dataclasses.replace(res, valid_until=normalize_date(by_key[spec["validity_field"]].raw_value))
            auth, specs = res.auth_result, list(res.evidence)
            d.update(valid_until=res.valid_until, problems=list(res.problems), flags=list(res.flags), issuer=res.issuer_id,
                     checks=[{"name": c.name, "passed": c.passed, "detail": c.detail} for c in res.checks])
            for sr in res.source_responses:
                sr2 = sr.model_copy(update={"id": new_id("sr")})
                ledger.record_source_response(conn, sr2, case_id)
                sr_map[sr.id] = sr2.id
                d["responses"].append(sr2)
        elif kind == "self_declared":
            auth = AuthResult.NOT_SOFTWARE_VERIFIABLE
            specs = [EvidenceSpec("self_declared", Strength.WEAK, False, "declared by the applicant; no independent proof")]
            d["problems"] = ["self-declared: no independent proof exists for this document type"]
        else:   # attestation: registry lookup is not built in this version
            auth = AuthResult.NOT_SOFTWARE_VERIFIABLE
            specs = [EvidenceSpec("attestation_not_checked", Strength.WEAK, False,
                                  "chartered-accountant attestation lookup is not available in this version")]
            d["problems"] = ["attestation registry check is not available in this version; manual review needed"]
            d["required_sources"] = []
        db.set_document_auth(conn, doc.id, auth)
        for c in d["claims"]:
            for s in specs:
                ledger.record_evidence(conn, Evidence(
                    id=new_id("evd"), claim_id=c.id, kind=s.kind, strength=s.strength, qualifies=s.qualifies,
                    detail=s.detail, source_response_id=sr_map.get(s.source_response_id) if s.source_response_id else None))

    # ---- 3. cross-document checks -----------------------------------------
    claims = db.list_claims(conn, case_id)
    doc_types = {d["doc"].id: d["doc"].doc_type for d in docs}
    issues: list[dict] = list(issues_pre)

    def first(dt: str, key: str):
        return next((c for c in claims if doc_types.get(c.document_id) == dt and c.field_key == key), None)

    reg_claim = first("society_registration", "document_number") or first("application_form", "registration_number")
    refs: dict = {}
    if reg_claim:
        if cfg.aicte_snapshot_path:
            regulator = AicteSnapshot.load(cfg.aicte_snapshot_path, outage=aicte_outage)
            reg_label = regulator.label
            stale = regulator.age_note(today)
            if stale:
                issues.append({"kind": "regulator_snapshot_old", "text": stale})
        else:
            regulator, reg_label = MockAicte(cfg.aicte, outage=aicte_outage), "mock AICTE"
        resp = regulator.lookup(reg_claim.raw_value)
        ledger.record_source_response(conn, resp.model_copy(update={"id": new_id("sr")}), case_id)
        refs = references_for(resp)
        if not resp.ok:
            issues.append({"kind": "regulator_unavailable", "text": f"{reg_label} could not be reached ({resp.error}); intake and name were not compared with the regulator"})
        elif not resp.payload.get("found"):
            issues.append({"kind": "regulator_no_record", "text": f"{reg_label} has no record for '{reg_claim.raw_value}'; intake and name were not compared with the regulator"})
    else:
        issues.append({"kind": "regulator_skipped", "text": "no registration number was read, so the regulator record was not consulted"})

    results = key_resolver.resolve(claims, doc_types, reg, refs)
    chain = chain_checks.check_chain(claims, doc_types, reg)
    issues += [{"kind": f"chain_{i.kind}", "text": i.description} for i in chain]

    area_rule = rule_store.effective(conn, "area_per_seat", today)
    built = first("application_form", "built_up_area_sqm")
    intake = first("application_form", "approved_intake") or first("state_noc", "approved_intake")
    area = check_area(built, intake, first("building_plan", "built_up_area_sqm"), area_rule)
    issues += [{"kind": f"area_{a.kind}", "text": a.description} for a in area]

    values: dict[str, str] = {}
    for c in sorted(claims, key=lambda c: doc_types.get(c.document_id) != "application_form"):
        values.setdefault(c.field_key, c.raw_value)
    rule_conflicts: dict[str, list[Conflict]] = {}
    rule_version: dict[str, str] = {}
    if area_rule:
        rule_version.update(built_up_area_sqm=area_rule.version, approved_intake=area_rule.version)
    for rr in rule_engine.evaluate_all(conn, today, values):
        rule = rule_store.effective(conn, rr.rule_id, today)
        field = rule.definition.get("field")
        rule_version[field] = rr.rule_version
        if rr.passed is False:
            rule_conflicts.setdefault(field, []).append(Conflict(rr.reason))
        elif rr.passed is None:
            issues.append({"kind": "rule_unjudged", "text": rr.reason})
    for rf in reuse.find_reuse(conn, case_id):
        issues.append({"kind": f"reuse_{rf.kind}", "text": rf.description})

    # ---- 4. decide every claim --------------------------------------------
    findings = []
    by_doc = {d["doc"].id: d for d in docs}
    for c in claims:
        d = by_doc[c.document_id]
        cons = (key_resolver.conflicts_for_claim(results, c.id) + chain_checks.issues_for_claim(chain, c.id)
                + area_conflicts(area, c.id) + rule_conflicts.get(c.field_key, []))
        cur = db.get_document(conn, d["doc"].id)
        inp = EvalInput(claim=c, field_key=c.field_key, auth_result=cur.auth_result, quality_ok=_quality_ok(d, c), today=today,
                        valid_until=d["valid_until"], conflicts=tuple(cons), evidence=tuple(db.list_evidence(conn, c.id)),
                        required_sources=tuple(d["required_sources"]), source_responses=tuple(d["responses"]),
                        rule_version=rule_version.get(c.field_key))
        f = ledger.record_finding(conn, to_finding(evaluate(inp), c.id, new_id("fnd"), rule_version.get(c.field_key)))
        fields = d["spec"]["fields"]
        findings.append({"finding_id": f.id, "claim_id": c.id, "document_id": c.document_id, "doc_type": d["doc"].doc_type,
                         "doc_label": d["spec"]["label"], "field_key": c.field_key,
                         "field_label": fields.get(c.field_key, {}).get("label", c.field_key), "value": c.raw_value,
                         "page": c.page, "bbox": list(c.bbox), "method": c.extraction_method, "confidence": c.ocr_confidence, "status": f.status.value, "reason": f.reason,
                         "evidence_ids": list(f.evidence_ids)})

    required = reg["categories"][category]["required_documents"]
    present = set(doc_types.values())
    missing = [{"doc_type": t, "label": reg["document_types"][t]["label"], "status": "Missing"}
               for t in required if t not in present]
    order = {t: i for i, t in enumerate(reg["document_types"])}
    findings.sort(key=lambda f: (order[f["doc_type"]], f["document_id"]))

    return {
        "case_id": case_id, "category": category, "date": today.isoformat(),
        "institution": institution_name[:MAX_NAME], "created_at": utcnow().isoformat(),
        "documents": [{"id": d["doc"].id, "filename": d["doc"].filename, "doc_type": d["doc"].doc_type, "sha256": d["doc"].sha256,
                       "label": d["spec"]["label"], "auth_result": db.get_document(conn, d["doc"].id).auth_result.value,
                       "issuer": d["issuer"], "checks": d["checks"], "problems": d["problems"], "flags": d["flags"],
                       "pages": d["doc"].pages, "scanned": d["scan"],
                       "page_quality": [{"page": q.page, "ok": q.ok, "confidence": q.mean_conf, "reason": q.reason}
                                        for q in d["quality"].values()]} for d in sorted(docs, key=lambda d: order[d["doc"].doc_type])],
        "findings": findings, "counts": ledger.status_counts(conn, case_id), "missing_documents": missing,
        "case_issues": issues, "unrecognised_files": unrecognised, "rejected_values": rejected,
        "notes": [_data_note(cfg),
                  "Rules are illustrative until checked against the official Handbook.",
                  "Scans are read by OCR (English, Hindi and Tamil where installed); pages that fail the quality gate are reported as Blurry scan, never guessed."],
    }
