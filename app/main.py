"""
main.py -- FastAPI server, role-checked authentication, live processing API,
DigiLocker integration, retention management, and web UI.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import os
import secrets
import time
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import auth, db, digilocker
from app.config_model import CustomiseConfig, PRESETS, PRESET_COLLEGE
from app.engine import run_processing_pipeline

ROOT = os.path.join(os.path.dirname(__file__), "..")
WEB = os.path.join(ROOT, "web")

app = FastAPI(title="Docroc Document Verification", docs_url=None, redoc_url=None)

# Database handle
_conn = db.connect()
db.init_db(_conn)


def get_actor(request: Request) -> Optional[auth.Actor]:
    cookie = request.cookies.get(auth.COOKIE)
    actor = auth.get_session_actor(cookie)
    if not actor and not auth.enabled():
        return auth.LOCAL_ADMIN
    return actor


def require_actor(request: Request, *allowed_roles: str) -> auth.Actor:
    actor = get_actor(request)
    if not actor:
        raise HTTPException(401, "Sign in required.")
    if allowed_roles and actor.role not in allowed_roles:
        raise HTTPException(403, "You do not have permission to perform this action.")
    return actor


# ---- Health & Me -----------------------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": "docroc"}


@app.get("/api/me")
async def api_me(request: Request):
    actor = get_actor(request)
    return {
        "auth": auth.enabled(),
        "signup": auth.signup_allowed(),
        "user": actor.public() if actor else None,
    }


# ---- Authentication (Role-Checked) -----------------------------------------------------------------------

class LoginPayload(BaseModel):
    username: str
    password: str
    role: str       # "applicant", "reviewer", or "admin"


@app.post("/api/login")
async def api_login(request: Request, body: LoginPayload, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    u = auth.normalise_username(body.username)
    role = (body.role or "").strip().lower()

    # Rate limiting lockout check
    lockout_key = f"{client_ip}:{u}"
    if auth.is_locked_out(lockout_key):
        raise HTTPException(
            429,
            "Too many failed sign-in attempts. Account temporarily locked for 10 minutes.",
            headers={"Retry-After": "600"},
        )

    generic_error = "Invalid username, password, or role."

    # Look up user
    user = db.get_user_by_username(_conn, u)
    if not user:
        auth.verify_unknown(body.password)
        auth.record_failure(lockout_key)
        raise HTTPException(401, generic_error)

    # Password check
    pw_ok = auth.verify_password(body.password, user["password_hash"])
    if not pw_ok or user["disabled"]:
        auth.record_failure(lockout_key)
        raise HTTPException(401, generic_error)

    # CRITICAL SECURITY RULE: The role picked on the sign-in page MUST match the account role!
    if user["role"] != role:
        auth.record_failure(lockout_key)
        raise HTTPException(401, generic_error)

    # Success: clear failure counters and start session
    auth.clear_failures(lockout_key)
    actor = auth.Actor(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        role=user["role"],
    )
    token = auth.start_session(actor)

    resp = JSONResponse({"user": actor.public()})
    resp.set_cookie(
        auth.COOKIE,
        token,
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"signed_in": False})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


class SignupPayload(BaseModel):
    username: str
    display_name: str
    password: str


@app.post("/api/signup")
async def api_signup(request: Request, body: SignupPayload):
    if not auth.signup_allowed():
        raise HTTPException(403, "Public applicant sign-up is currently disabled by administrator.")

    ok, msg, user = db.create_user(
        _conn,
        username=body.username,
        display_name=body.display_name,
        role="applicant",       # Self-signup ALWAYS creates applicant only
        password=body.password,
        created_by="self-signup",
    )
    if not ok:
        raise HTTPException(400, msg)

    actor = auth.Actor(
        id=user["id"],
        username=user["username"],
        display_name=user["display_name"],
        role=user["role"],
    )
    token = auth.start_session(actor)
    resp = JSONResponse({"user": actor.public()})
    resp.set_cookie(
        auth.COOKIE,
        token,
        max_age=auth.SESSION_TTL,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return resp


# ---- Customise Configuration & Presets -------------------------------------------------------------------

@app.get("/api/config")
async def get_config(request: Request):
    actor = get_actor(request)
    cfg = db.get_customise_config(_conn)

    # If applicant or unauthenticated, return sanitized checklist only
    if not actor or actor.is_applicant:
        return {
            "institution_name": cfg.institution_name,
            "brand_color": cfg.brand_color,
            "logo_text": cfg.logo_text,
            "documents": cfg.sanitized_checklist(),
        }

    # Reviewer or Admin gets full configuration model & available presets
    return {
        "config": cfg.model_dump(),
        "available_presets": list(PRESETS.keys()),
    }


class UpdateConfigPayload(BaseModel):
    preset_name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


@app.post("/api/config")
async def update_config(request: Request, body: UpdateConfigPayload):
    require_actor(request, "admin")

    if body.preset_name and body.preset_name in PRESETS:
        new_cfg = PRESETS[body.preset_name]
        db.save_customise_config(_conn, new_cfg)
        return {"ok": True, "message": f"Applied preset '{body.preset_name}'.", "config": new_cfg.model_dump()}

    if body.config:
        try:
            parsed = CustomiseConfig.model_validate(body.config)
            db.save_customise_config(_conn, parsed)
            return {"ok": True, "message": "Customisation settings saved.", "config": parsed.model_dump()}
        except Exception as e:
            raise HTTPException(400, f"Invalid configuration schema: {str(e)}")

    raise HTTPException(400, "Please provide either a valid preset_name or config object.")


# ---- Submissions & Processing ----------------------------------------------------------------------------

@app.get("/api/submissions")
async def list_submissions(request: Request):
    actor = require_actor(request)
    if actor.is_applicant:
        subs = db.list_submissions(_conn, applicant_id=actor.id)
    else:
        subs = db.list_submissions(_conn)
    return subs


@app.post("/api/submissions")
async def create_submission_upload(
    request: Request,
    files: List[UploadFile] = File(...),
):
    actor = require_actor(request)
    cfg = db.get_customise_config(_conn)

    # Read uploaded files into memory
    uploads: Dict[str, Tuple[str, bytes]] = {}
    for f in files:
        doc_id = f.filename.split(".")[0].lower()       # e.g., "marksheet_10" from "marksheet_10.pdf"
        # Match doc_id to configured documents or use first matching doc_def
        matched_id = None
        for d in cfg.documents:
            if d.id.lower() in doc_id or doc_id in d.id.lower():
                matched_id = d.id
                break
        if not matched_id and cfg.documents:
            # Fallback to unmatched order or key
            matched_id = doc_id

        data = await f.read()
        if len(data) > 0 and matched_id:
            uploads[matched_id] = (f.filename, data)

    if not uploads:
        raise HTTPException(400, "No valid files received.")

    # Execute processing engine pipeline
    report = await run_processing_pipeline(cfg, uploads)

    sub_id = secrets.token_hex(8)
    db.create_submission(
        _conn,
        submission_id=sub_id,
        applicant_id=actor.id,
        applicant_name=actor.display_name,
        institution=cfg.institution_name,
        status=report["verdict"],
        report=report,
        files=uploads,
        delivery_mode=cfg.delivery_mode,
    )

    # Initial system audit record
    db.record_review_action(
        _conn,
        submission_id=sub_id,
        actor_id="system",
        actor_name="Automated Checker",
        action_kind="verification_run",
        note=f"Automatic check produced verdict: {report['verdict']}.",
    )

    return {
        "id": sub_id,
        "verdict": report["verdict"],
        "report": report,
        "delivery_mode": cfg.delivery_mode,
    }


@app.get("/api/submissions/{submission_id}")
async def get_submission_detail(request: Request, submission_id: str):
    actor = require_actor(request)
    sub = db.get_submission(_conn, submission_id)
    if not sub:
        raise HTTPException(404, "Submission not found.")

    if actor.is_applicant and sub["applicant_id"] != actor.id:
        raise HTTPException(403, "Access denied.")

    actions = db.list_review_actions(_conn, submission_id)
    sub["audit_actions"] = actions
    return sub


class ReviewActionPayload(BaseModel):
    action_kind: Literal["comment", "request_clarification", "mark_checked", "approve", "reject"]
    note: str


@app.post("/api/submissions/{submission_id}/action")
async def post_review_action(request: Request, submission_id: str, body: ReviewActionPayload):
    actor = require_actor(request, "reviewer", "admin")
    sub = db.get_submission(_conn, submission_id)
    if not sub:
        raise HTTPException(404, "Submission not found.")

    record = db.record_review_action(
        _conn,
        submission_id=submission_id,
        actor_id=actor.id,
        actor_name=actor.label,
        action_kind=body.action_kind,
        note=body.note.strip(),
    )
    return {"ok": True, "action": record}


@app.get("/api/submissions/{submission_id}/slip")
async def get_submission_slip(request: Request, submission_id: str):
    actor = require_actor(request)
    sub = db.get_submission(_conn, submission_id)
    if not sub:
        raise HTTPException(404, "Submission not found.")

    if actor.is_applicant and sub["applicant_id"] != actor.id:
        raise HTTPException(403, "Access denied.")

    report = sub.get("report", {})
    verdict = sub["status"]
    color = "#16a34a" if verdict == "Cleared" else ("#ca8a04" if verdict == "Review" else "#dc2626")

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Verification Result Slip · {sub['id']}</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; padding: 40px; color: #1e293b; max-width: 800px; margin: 0 auto; }}
  .header {{ border-bottom: 2px solid #e2e8f0; padding-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }}
  .verdict-badge {{ font-size: 20px; font-weight: 700; color: white; background: {color}; padding: 8px 18px; border-radius: 8px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin: 24px 0; }}
  .item {{ background: #f8fafc; padding: 12px 16px; border-radius: 6px; }}
  .item b {{ display: block; font-size: 12px; color: #64748b; margin-bottom: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
  th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #e2e8f0; font-size: 14px; }}
  th {{ background: #f1f5f9; }}
  @media print {{ .noprint {{ display: none; }} }}
</style>
</head>
<body>
<div class="header">
  <div>
    <h1 style="margin:0 0 6px 0;">{sub['institution']}</h1>
    <p style="margin:0; color:#64748b;">Official Document Verification Report</p>
  </div>
  <div class="verdict-badge">{verdict}</div>
</div>

<div class="grid">
  <div class="item"><b>SUBMISSION ID</b> {sub['id']}</div>
  <div class="item"><b>APPLICANT NAME</b> {sub['applicant_name']}</div>
  <div class="item"><b>DATE PROCESSED</b> {datetime.datetime.fromtimestamp(sub['created_at']).strftime('%Y-%m-%d %H:%M UTC')}</div>
  <div class="item"><b>DATA RETENTION</b> {'Files Wiped (Data-Only Mode)' if sub['files_wiped'] else 'Files Stored Securely'}</div>
</div>

<h3>Document Checks</h3>
<table>
  <thead><tr><th>Document</th><th>Status</th><th>Legibility</th><th>Digital Signature</th></tr></thead>
  <tbody>
    {''.join(f"<tr><td>{d.get('title')}</td><td>{d.get('status').title()}</td><td>{'Good' if d.get('is_legible') else 'Unreadable'}</td><td>{'Present' if d.get('has_digital_signature') else 'None'}</td></tr>" for d in report.get('documents', []))}
  </tbody>
</table>

<h3>Cross-Document Checks</h3>
<table>
  <thead><tr><th>Check</th><th>Match Type</th><th>Outcome</th><th>Details</th></tr></thead>
  <tbody>
    {''.join(f"<tr><td>{c.get('group_name')}</td><td>{c.get('match_type')}</td><td>{'Matched' if c.get('matched') else 'Discrepancy'}</td><td>{c.get('reason')}</td></tr>" for c in report.get('cross_checks', []))}
  </tbody>
</table>

<div class="noprint" style="margin-top: 30px;">
  <button onclick="window.print()" style="padding: 10px 20px; font-weight: 600; cursor: pointer; border-radius: 6px; background: #2250d8; color: white; border: none;">Print Result Slip</button>
</div>
</body>
</html>"""
    return HTMLResponse(html)


# ---- User Management (Admin Only) ------------------------------------------------------------------------

@app.get("/api/users")
async def api_list_users(request: Request):
    require_actor(request, "admin")
    return db.list_users(_conn)


class CreateUserPayload(BaseModel):
    username: str
    display_name: str
    role: str
    password: str


@app.post("/api/users")
async def api_create_user(request: Request, body: CreateUserPayload):
    admin = require_actor(request, "admin")
    ok, msg, user = db.create_user(
        _conn,
        username=body.username,
        display_name=body.display_name,
        role=body.role,
        password=body.password,
        created_by=admin.label,
    )
    if not ok:
        raise HTTPException(400, msg)
    return user


class UpdateUserPayload(BaseModel):
    display_name: Optional[str] = None
    role: Optional[str] = None
    disabled: Optional[bool] = None
    password: Optional[str] = None


@app.patch("/api/users/{user_id}")
async def api_update_user(request: Request, user_id: str, body: UpdateUserPayload):
    require_actor(request, "admin")
    ok, msg = db.update_user(
        _conn,
        user_id=user_id,
        display_name=body.display_name,
        role=body.role,
        disabled=body.disabled,
        password=body.password,
    )
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


@app.delete("/api/users/{user_id}")
async def api_delete_user(request: Request, user_id: str):
    require_actor(request, "admin")
    ok, msg = db.delete_user(_conn, user_id)
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


# ---- Web UI ----------------------------------------------------------------------------------------------

@app.get("/login")
async def serve_login(request: Request):
    return FileResponse(os.path.join(WEB, "login.html"))


@app.get("/")
async def serve_root(request: Request):
    return FileResponse(os.path.join(WEB, "reviewer.html"))
