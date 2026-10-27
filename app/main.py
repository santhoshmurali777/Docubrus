"""
main.py -- the web API and the reviewer page.

PURPOSE   Serve web/reviewer.html and the JSON endpoints: list the seeded scenarios, run one,
          upload your own PDFs, fetch a stored report, list cases worst-first (triage), serve a page of an
          original PDF as an image (so a finding can be shown on the page), and record reviewer actions.
RUN       uvicorn app.main:app --reload      then open http://127.0.0.1:8000
IMPORTS   orchestrator, db, ledger, pages, triage
NOTE      No login yet (auth is not built in this version): run it locally, do not expose it.
          Seeded scenarios run in a throw-away database each time, so reruns never look like
          certificate reuse. Uploads share one database so cross-application reuse is visible.
          Handlers are async on purpose: SQLite connections stay on the event-loop thread.
          Reports and the original PDFs are held in memory only (bounded); reviewer actions go to the shared
          database, so with SAAKSHYA_DB=<file> they survive a restart (the cases themselves do not, until W7).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from collections import OrderedDict
from typing import List, Literal, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from app import db, ledger, pages, triage
from app.contracts import ReviewAction, ReviewKind
from app.documents.classify import classify_pdf, load_signatures
from app.orchestrator import DEFAULT_CATEGORY, Upload, analyze

ROOT = os.path.join(os.path.dirname(__file__), "..")
SYN = os.path.join(ROOT, "data", "synthetic")
WEB = os.path.join(ROOT, "web")
MAX_FILES, MAX_BYTES = 15, 10 * 1024 * 1024
MAX_REPORTS = 50
MAX_STORED_BYTES = 150 * 1024 * 1024      # original PDFs kept for the page viewer, oldest case dropped first

app = FastAPI(title="Docubrus (prototype)", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    """Optional shared password. Active only when SAAKSHYA_USER and SAAKSHYA_PASSWORD are both set.
    /healthz stays open so a hosting platform can check the service. This is one shared login, not
    per-reviewer accounts: the reviewer name typed on the page is still not verified."""
    user, pw = os.environ.get("SAAKSHYA_USER"), os.environ.get("SAAKSHYA_PASSWORD")
    if not (user and pw) or request.url.path == "/healthz":
        return await call_next(request)
    ok = False
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            u, _, p = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            ok = secrets.compare_digest(u.encode(), user.encode()) & secrets.compare_digest(p.encode(), pw.encode())
        except Exception:
            ok = False
    if ok:
        return await call_next(request)
    return Response("Sign in required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Docubrus"'})


_conn = None
_reports: "OrderedDict[str, dict]" = OrderedDict()
_files: "OrderedDict[str, dict[str, bytes]]" = OrderedDict()     # case_id -> document_id -> PDF bytes


def shared_conn():
    global _conn
    if _conn is None:
        _conn = db.connect(os.environ.get("SAAKSHYA_DB", ":memory:"))
        db.init_db(_conn)
    return _conn


def manifest() -> list[dict]:
    with open(os.path.join(SYN, "manifest.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _stored_bytes() -> int:
    return sum(len(b) for docs in _files.values() for b in {id(x): x for x in docs.values()}.values())


def keep(report: dict, uploads: Optional[list] = None) -> dict:
    """Remember a report and the original PDFs it was made from (so pages can be shown later)."""
    if uploads:
        by_sha = {hashlib.sha256(u.data).hexdigest(): u.data for u in uploads}
        held = {}
        for d in report["documents"]:
            data = by_sha.get(d["sha256"])
            d["page_sizes"] = []
            if data is not None:
                held[d["id"]] = data
                try:
                    d["page_sizes"] = pages.page_sizes(data)
                except Exception:               # the page viewer is a convenience; never fail a check for it
                    pass
        _files[report["case_id"]] = held
        while len(_files) > 1 and _stored_bytes() > MAX_STORED_BYTES:
            _files.popitem(last=False)
    _reports[report["case_id"]] = report
    while len(_reports) > MAX_REPORTS:
        gone, _ = _reports.popitem(last=False)
        _files.pop(gone, None)
    return report


def _run(conn, uploads, outage, aicte_outage, name) -> dict:
    try:
        return keep(analyze(conn, uploads, institution_name=name, outages=tuple(outage), aicte_outage=aicte_outage,
                            category=DEFAULT_CATEGORY), uploads)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _case(case_id: str) -> dict:
    if case_id not in _reports:
        raise HTTPException(404, "no such case (reports are kept in memory only)")
    return _reports[case_id]


def _attention(report: dict) -> int:
    c = report["counts"]
    return c.get("Contradicted", 0) + c.get("Expired", 0) + len(report["missing_documents"])


@app.get("/")
async def home():
    return FileResponse(os.path.join(WEB, "reviewer.html"), headers={"Cache-Control": "no-store"})


@app.get("/api/scenarios")
async def scenarios():
    return [{"id": s["id"], "title": s["title"], "what": s["what"], "files": len(s["files"])} for s in manifest()]


@app.post("/api/demo/{scenario_id}")
async def demo(scenario_id: str, outage: List[str] = Query(default=[]), aicte_outage: bool = False):
    sc = next((s for s in manifest() if s["id"] == scenario_id), None)    # whitelist: no path built from user input
    if sc is None:
        raise HTTPException(404, "unknown scenario")
    folder = os.path.join(SYN, sc["id"])
    uploads = [Upload(f"{dt}.pdf", open(os.path.join(folder, f"{dt}.pdf"), "rb").read()) for dt in sc["files"]]
    conn = db.connect(); db.init_db(conn)
    try:
        return _run(conn, uploads, outage, aicte_outage, sc["title"])
    finally:
        conn.close()


@app.post("/api/cases")
async def upload_case(files: List[UploadFile] = File(...), institution_name: str = Query("(unnamed)", max_length=120),
                      outage: List[str] = Query(default=[]), aicte_outage: bool = False):
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"send between 1 and {MAX_FILES} PDF files")
    uploads = []
    for f in files:
        data = await f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{f.filename}: larger than {MAX_BYTES // (1024 * 1024)} MB")
        uploads.append(Upload(f.filename or "upload.pdf", data))
    return _run(shared_conn(), uploads, outage, aicte_outage, institution_name)


@app.get("/api/cases")
async def list_cases():
    """Triage list: every case held in memory, highest risk first."""
    conn = shared_conn()
    rows = [triage.summarise(r, ledger.review_state(conn, cid), r.get("institution", "(unnamed)"), r.get("created_at", ""))
            for cid, r in _reports.items()]
    return triage.sort_worst_first(rows)


@app.get("/api/cases/{case_id}")
async def get_case(case_id: str):
    return _case(case_id)


@app.get("/api/cases/{case_id}/documents/{document_id}/pages/{page}")
async def page_image(case_id: str, document_id: str, page: int):
    """One page of an original PDF as a PNG, drawn at the same proportions as the claim boxes."""
    report = _case(case_id)
    doc = next((d for d in report["documents"] if d["id"] == document_id), None)
    data = _files.get(case_id, {}).get(document_id)
    if doc is None or page < 1 or page > doc["pages"]:
        raise HTTPException(404, "no such page")
    if data is None:
        raise HTTPException(404, "the original PDF is no longer held by the server")
    try:
        png = pages.render_png(data, doc["sha256"], page)
    except IndexError:
        raise HTTPException(404, "no such page")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


# ---- reviewer actions ------------------------------------------------------
class ActionIn(BaseModel):
    kind: Literal["comment", "clarify", "resolve", "attest", "approve"]
    actor: str = Field(max_length=80)
    note: str = Field(default="", max_length=2000)
    finding_id: Optional[str] = None
    resolves_id: Optional[str] = None


def _review_payload(conn, case_id: str) -> dict:
    return {"state": ledger.review_state(conn, case_id),
            "actions": [a.model_dump(mode="json") for a in db.list_review_actions(conn, case_id)]}


@app.get("/api/cases/{case_id}/review")
async def get_review(case_id: str):
    _case(case_id)
    return _review_payload(shared_conn(), case_id)


@app.post("/api/cases/{case_id}/actions")
async def post_action(case_id: str, body: ActionIn):
    report = _case(case_id)
    conn = shared_conn()
    try:
        action = ReviewAction(id=ledger.new_id("act"), case_id=case_id, finding_id=body.finding_id,
                              kind=ReviewKind(body.kind), actor=body.actor.strip(), note=body.note.strip(),
                              resolves_id=body.resolves_id)
        ledger.record_review_action(conn, action, case_finding_ids={f["finding_id"] for f in report["findings"]},
                                    attention=_attention(report))
    except ValidationError as e:
        raise HTTPException(400, e.errors()[0]["msg"].removeprefix("Value error, "))
    except ledger.LedgerError as e:
        raise HTTPException(409, str(e))
    return {"action": action.model_dump(mode="json"), **_review_payload(conn, case_id)}


@app.post("/api/classify")
async def classify(files: List[UploadFile] = File(...)):
    """Tell what each PDF is and what proof it carries. Does not verify anything."""
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"send between 1 and {MAX_FILES} PDF files")
    sigs, out = load_signatures(), []
    for f in files:
        data = await f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{f.filename}: larger than {MAX_BYTES // (1024 * 1024)} MB")
        out.append({"filename": f.filename, **classify_pdf(data, sigs).as_dict()})
    return out


@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})
