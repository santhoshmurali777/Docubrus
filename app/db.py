"""
db.py -- SQLite database for accounts, customisation config, submissions, audit trail, and file delivery.

Tables:
  - users: id, username, display_name, role, password_hash, disabled, created_at, created_by
  - settings: key, value_json, updated_at
  - submissions: id, applicant_id, applicant_name, institution, status, created_at, report_json, files_wiped
  - submission_files: id, submission_id, doc_id, filename, content_blob
  - review_actions: id, submission_id, actor_id, actor_name, action_kind, note, created_at
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from app import auth
from app.config_model import CustomiseConfig, PRESET_COLLEGE


def get_db_path() -> str:
    return os.environ.get("DOCROC_DB") or os.environ.get("SAAKSHYA_DB") or "docroc.db"


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    db_path = path or get_db_path()
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            disabled INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            created_by TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id TEXT PRIMARY KEY,
            applicant_id TEXT NOT NULL,
            applicant_name TEXT NOT NULL,
            institution TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            report_json TEXT NOT NULL,
            files_wiped INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS submission_files (
            id TEXT PRIMARY KEY,
            submission_id TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content_blob BLOB NOT NULL,
            FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS review_actions (
            id TEXT PRIMARY KEY,
            submission_id TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            actor_name TEXT NOT NULL,
            action_kind TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE
        );
        """)

    # Ensure default customise config is seeded
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'customise_config'")
    row = cur.fetchone()
    if not row:
        with conn:
            conn.execute(
                "INSERT INTO settings (key, value_json, updated_at) VALUES (?, ?, ?)",
                ("customise_config", PRESET_COLLEGE.model_dump_json(), time.time()),
            )

    # Ensure initial admin user exists
    bootstrap_admin(conn)


def bootstrap_admin(conn: sqlite3.Connection) -> None:
    admin_user, admin_pw = auth.admin_credentials()
    if not admin_user:
        admin_user = "admin"
    if not admin_pw:
        admin_pw = "Admin123!"

    cur = conn.execute("SELECT id FROM users WHERE username = ?", (admin_user,))
    row = cur.fetchone()
    if not row:
        user_id = secrets.token_hex(8)
        pw_hash = auth.hash_password(admin_pw)
        with conn:
            conn.execute(
                "INSERT INTO users (id, username, display_name, role, password_hash, created_at, created_by) "
                "VALUES (?, ?, ?, 'admin', ?, ?, 'system')",
                (user_id, admin_user, "Default Administrator", pw_hash, time.time()),
            )


# ---- User Operations -------------------------------------------------------------------------------------

def get_user_by_username(conn: sqlite3.Connection, username: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cur.fetchone()
    return dict(row) if row else None


def get_user_by_id(conn: sqlite3.Connection, user_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def list_users(conn: sqlite3.Connection) -> List[dict]:
    cur = conn.execute("SELECT id, username, display_name, role, disabled, created_at, created_by FROM users ORDER BY created_at ASC")
    return [dict(r) for r in cur.fetchall()]


def count_active_admins(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin' AND disabled = 0")
    return cur.fetchone()["c"]


def create_user(
    conn: sqlite3.Connection,
    username: str,
    display_name: str,
    role: str,
    password: str,
    created_by: str = "system",
) -> Tuple[bool, str, Optional[dict]]:
    u = auth.normalise_username(username)
    err = auth.check_username(u)
    if err:
        return False, err, None
    err = auth.check_new_password(password)
    if err:
        return False, err, None
    if role not in auth.ROLES:
        return False, f"Role must be one of: {', '.join(auth.ROLES)}", None

    if get_user_by_username(conn, u):
        return False, "Username already exists.", None

    uid = secrets.token_hex(8)
    pw_hash = auth.hash_password(password)
    now = time.time()
    with conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, password_hash, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, u, display_name.strip() or u, role, pw_hash, now, created_by),
        )
    return True, "User created successfully.", get_user_by_id(conn, uid)


def update_user(
    conn: sqlite3.Connection,
    user_id: str,
    display_name: Optional[str] = None,
    role: Optional[str] = None,
    disabled: Optional[bool] = None,
    password: Optional[str] = None,
) -> Tuple[bool, str]:
    user = get_user_by_id(conn, user_id)
    if not user:
        return False, "User not found."

    # Protected last admin check
    if user["role"] == "admin" and not user["disabled"]:
        if (role and role != "admin") or (disabled is True):
            if count_active_admins(conn) <= 1:
                return False, "Cannot disable or demote the last active administrator."

    updates = []
    params = []
    if display_name is not None:
        updates.append("display_name = ?")
        params.append(display_name.strip() or user["username"])
    if role is not None:
        if role not in auth.ROLES:
            return False, f"Invalid role: {role}"
        updates.append("role = ?")
        params.append(role)
    if disabled is not None:
        updates.append("disabled = ?")
        params.append(1 if disabled else 0)
    if password is not None:
        err = auth.check_new_password(password)
        if err:
            return False, err
        updates.append("password_hash = ?")
        params.append(auth.hash_password(password))

    if not updates:
        return True, "No changes made."

    params.append(user_id)
    with conn:
        conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    return True, "User updated successfully."


def delete_user(conn: sqlite3.Connection, user_id: str) -> Tuple[bool, str]:
    user = get_user_by_id(conn, user_id)
    if not user:
        return False, "User not found."
    if user["role"] == "admin" and not user["disabled"]:
        if count_active_admins(conn) <= 1:
            return False, "Cannot delete the last active administrator."

    with conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return True, "User deleted successfully."


# ---- Settings & Customisation ----------------------------------------------------------------------------

def get_customise_config(conn: sqlite3.Connection) -> CustomiseConfig:
    cur = conn.execute("SELECT value_json FROM settings WHERE key = 'customise_config'")
    row = cur.fetchone()
    if row:
        try:
            return CustomiseConfig.model_validate_json(row["value_json"])
        except Exception:
            pass
    return PRESET_COLLEGE


def save_customise_config(conn: sqlite3.Connection, config: CustomiseConfig) -> None:
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value_json, updated_at) VALUES ('customise_config', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (config.model_dump_json(), time.time()),
        )


# ---- Submissions & Files ---------------------------------------------------------------------------------

def create_submission(
    conn: sqlite3.Connection,
    submission_id: str,
    applicant_id: str,
    applicant_name: str,
    institution: str,
    status: str,
    report: dict,
    files: Optional[Dict[str, Tuple[str, bytes]]] = None,
    delivery_mode: str = "deliver_files",
) -> None:
    wipe = 1 if delivery_mode == "data_only" else 0
    now = time.time()
    with conn:
        conn.execute(
            "INSERT INTO submissions (id, applicant_id, applicant_name, institution, status, created_at, report_json, files_wiped) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (submission_id, applicant_id, applicant_name, institution, status, now, json.dumps(report), wipe),
        )
        if files and delivery_mode == "deliver_files":
            for doc_id, (fname, blob) in files.items():
                fid = secrets.token_hex(8)
                conn.execute(
                    "INSERT INTO submission_files (id, submission_id, doc_id, filename, content_blob) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (fid, submission_id, doc_id, fname, blob),
                )


def get_submission(conn: sqlite3.Connection, submission_id: str) -> Optional[dict]:
    cur = conn.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["report"] = json.loads(d["report_json"])
    return d


def list_submissions(conn: sqlite3.Connection, applicant_id: Optional[str] = None) -> List[dict]:
    if applicant_id:
        cur = conn.execute(
            "SELECT id, applicant_id, applicant_name, institution, status, created_at, files_wiped "
            "FROM submissions WHERE applicant_id = ? ORDER BY created_at DESC",
            (applicant_id,),
        )
    else:
        cur = conn.execute(
            "SELECT id, applicant_id, applicant_name, institution, status, created_at, files_wiped "
            "FROM submissions ORDER BY created_at DESC"
        )
    return [dict(r) for r in cur.fetchall()]


def get_submission_file(conn: sqlite3.Connection, submission_id: str, doc_id: str) -> Optional[Tuple[str, bytes]]:
    cur = conn.execute(
        "SELECT filename, content_blob FROM submission_files WHERE submission_id = ? AND doc_id = ?",
        (submission_id, doc_id),
    )
    row = cur.fetchone()
    if row:
        return row["filename"], row["content_blob"]
    return None


def purge_old_submissions(conn: sqlite3.Connection, retention_days: int) -> int:
    cutoff = time.time() - (retention_days * 86400)
    with conn:
        cur = conn.execute("DELETE FROM submissions WHERE created_at < ?", (cutoff,))
        return cur.rowcount


# ---- Audit Actions ---------------------------------------------------------------------------------------

def record_review_action(
    conn: sqlite3.Connection,
    submission_id: str,
    actor_id: str,
    actor_name: str,
    action_kind: str,
    note: str,
) -> dict:
    aid = secrets.token_hex(8)
    now = time.time()
    with conn:
        conn.execute(
            "INSERT INTO review_actions (id, submission_id, actor_id, actor_name, action_kind, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (aid, submission_id, actor_id, actor_name, action_kind, note, now),
        )
    return {
        "id": aid,
        "submission_id": submission_id,
        "actor_id": actor_id,
        "actor_name": actor_name,
        "action_kind": action_kind,
        "note": note,
        "created_at": now,
    }


def list_review_actions(conn: sqlite3.Connection, submission_id: str) -> List[dict]:
    cur = conn.execute(
        "SELECT * FROM review_actions WHERE submission_id = ? ORDER BY created_at ASC",
        (submission_id,),
    )
    return [dict(r) for r in cur.fetchall()]
