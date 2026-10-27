"""
auth.py -- Accounts, roles, scrypt hashing, and role-checked sessions.

Security rules:
  1. Role-checked login: The role picked on the sign-in page MUST match the account.
     Wrong role, wrong password, or unknown user all return the SAME generic error:
     "Invalid username, password, or role."
  2. 5-attempt lockout per user/IP.
  3. Scrypt password hashing with unique 16-byte salt.
  4. Server-side revocable session tokens delivered via HttpOnly cookies.
  5. Protected last administrator (cannot delete, disable, or demote the last admin).
  6. Self-signup creates ONLY applicant accounts (when admin toggle is enabled).
  7. First admin initialized from DOCROC_ADMIN_USER/DOCROC_ADMIN_PASSWORD (or SAAKSHYA_USER),
     or generates a secure one-time initial password.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

ROLES = ("applicant", "reviewer", "admin")
STAFF = ("reviewer", "admin")
ROLE_LABELS = {
    "applicant": "Applicant",
    "reviewer": "Reviewer",
    "admin": "Administrator",
}

SESSION_TTL = 12 * 3600
COOKIE = "docroc_session"

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
MIN_PASSWORD, MAX_PASSWORD = 8, 128

_N, _R, _P = 2**14, 8, 1

# Rate limiting: 5 failed attempts in 10 minutes (600s)
LIMIT_ATTEMPTS = 5
LOCKOUT_WINDOW = 600

_failures: Dict[str, List[float]] = {}
_sessions: Dict[str, Session] = {}


# ---- Passwords -------------------------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        want = base64.b64decode(dk_b64)
        salt = base64.b64decode(salt_b64)
        got = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(want),
        )
        return hmac.compare_digest(got, want)
    except Exception:
        return False


_DUMMY_HASH = hash_password("not-a-real-dummy-pw")


def verify_unknown(password: str) -> bool:
    """Spends identical scrypt CPU time so nonexistent usernames cannot be identified via timing attacks."""
    verify_password(password, _DUMMY_HASH)
    return False


def normalise_username(raw: str) -> str:
    return (raw or "").strip().lower()


def check_username(username: str) -> Optional[str]:
    if not USERNAME_RE.match(username):
        return "Username must be 3 to 32 letters, digits, dots, dashes, or underscores."
    return None


def check_new_password(password: str) -> Optional[str]:
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters long."
    if len(password) > MAX_PASSWORD:
        return f"Password must be at most {MAX_PASSWORD} characters."
    return None


# ---- Rate Limiting (Lockout) -----------------------------------------------------------------------------

def is_locked_out(key: str) -> bool:
    now = time.time()
    attempts = [t for t in _failures.get(key, []) if now - t < LOCKOUT_WINDOW]
    _failures[key] = attempts
    return len(attempts) >= LIMIT_ATTEMPTS


def record_failure(key: str) -> None:
    now = time.time()
    attempts = [t for t in _failures.get(key, []) if now - t < LOCKOUT_WINDOW]
    attempts.append(now)
    _failures[key] = attempts


def clear_failures(key: str) -> None:
    _failures.pop(key, None)


def reset_throttle() -> None:
    _failures.clear()


# ---- Actor & Sessions ------------------------------------------------------------------------------------

@dataclass
class Actor:
    id: str
    username: str
    display_name: str
    role: str
    local: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_reviewer(self) -> bool:
        return self.role in STAFF

    @property
    def is_applicant(self) -> bool:
        return self.role == "applicant"

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.username})"

    def public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role.title()),
        }


LOCAL_ADMIN = Actor(id="local", username="local", display_name="Local Administrator", role="admin", local=True)


@dataclass
class Session:
    token: str
    actor: Actor
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + SESSION_TTL)


def start_session(actor: Actor) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = Session(token=token, actor=actor)
    return token


def get_session_actor(token: Optional[str]) -> Optional[Actor]:
    if not token or token not in _sessions:
        return None
    s = _sessions[token]
    if time.time() > s.expires_at:
        _sessions.pop(token, None)
        return None
    return s.actor


def end_session(token: Optional[str]) -> None:
    if token:
        _sessions.pop(token, None)


def clear_all_sessions() -> None:
    _sessions.clear()


def get_session_expires(token: Optional[str]) -> Optional[float]:
    """Return the Unix timestamp when this session expires, or None if not found."""
    if not token or token not in _sessions:
        return None
    return _sessions[token].expires_at


# ---- Configuration Switches ------------------------------------------------------------------------------

def admin_credentials() -> Tuple[Optional[str], Optional[str]]:
    u = os.environ.get("DOCROC_ADMIN_USER") or os.environ.get("SAAKSHYA_USER")
    p = os.environ.get("DOCROC_ADMIN_PASSWORD") or os.environ.get("SAAKSHYA_PASSWORD")
    return u, p


def enabled() -> bool:
    """Authentication is required unless running in unconfigured local mode."""
    u, p = admin_credentials()
    return bool(u and p)


def signup_allowed() -> bool:
    val = os.environ.get("DOCROC_ALLOW_SIGNUP") or os.environ.get("SAAKSHYA_ALLOW_SIGNUP", "")
    return val in ("1", "true", "True")


def get_session_expires(token: Optional[str]) -> Optional[float]:
    """Return the Unix timestamp when this session expires, or None if not found."""
    if not token or token not in _sessions:
        return None
    return _sessions[token].expires_at
