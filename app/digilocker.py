"""
digilocker.py -- connect a user's DigiLocker and pull issued PDFs into a case.

PURPOSE   OAuth 2.0 (authorization code + PKCE) against DigiLocker, then list the user's issued
          documents and download the ones they pick. main.py exposes this as /digilocker/* routes.
SETUP     Register as a Requester on https://partners.digitallocker.gov.in (or API Setu) and set:
              DIGILOCKER_CLIENT_ID        the client id you were given
              DIGILOCKER_CLIENT_SECRET    the client secret (keep it on the server only)
              DIGILOCKER_REDIRECT_URI     https://<your-site>/digilocker/callback  (must match exactly
                                          what you registered with DigiLocker)
              DIGILOCKER_BASE_URL         optional, default https://api.digitallocker.gov.in
          For a demo with no credentials set DIGILOCKER_MOCK=1: a fake DigiLocker that offers the
          synthetic "clean" application. Never set it on a real deployment.
SECURITY  The access token never leaves the server. The browser only holds an opaque, HttpOnly
          session cookie. A file can only be fetched if it appeared in that session's own listing.
NOTE      Endpoint paths follow the DigiLocker Requester API specification; check them against the
          version DigiLocker gave you when you registered (paths below are the only place to change).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlencode

import httpx

ROOT = os.path.join(os.path.dirname(__file__), "..")
SYN = os.path.join(ROOT, "data", "synthetic")

DEFAULT_BASE = "https://api.digitallocker.gov.in"
PATH_AUTHORIZE = "/public/oauth2/1/authorize"
PATH_TOKEN = "/public/oauth2/1/token"
PATH_ISSUED = "/public/oauth2/1/files/issued"
PATH_FILE = "/public/oauth2/1/file/"

STATE_TTL = 600            # seconds the user has to finish signing in at DigiLocker
SESSION_TTL = 3600
MAX_SESSIONS = 200
MAX_LISTED = 200
HTTP_TIMEOUT = 20.0

_transport: Optional[httpx.AsyncBaseTransport] = None      # tests inject a fake DigiLocker here


class DigiLockerError(Exception):
    """A problem worth showing to the user. The message never contains secrets or tokens."""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    base: str
    mock: bool

    @property
    def configured(self) -> bool:
        return self.mock or bool(self.client_id and self.client_secret and self.redirect_uri)


def settings() -> Settings:
    g = os.environ.get
    return Settings(client_id=g("DIGILOCKER_CLIENT_ID", "").strip(),
                    client_secret=g("DIGILOCKER_CLIENT_SECRET", "").strip(),
                    redirect_uri=g("DIGILOCKER_REDIRECT_URI", "").strip(),
                    base=g("DIGILOCKER_BASE_URL", DEFAULT_BASE).strip().rstrip("/"),
                    mock=g("DIGILOCKER_MOCK", "").strip() == "1")


# ---- sessions (in memory, like the cases) ----------------------------------------------
@dataclass
class Session:
    created: float = field(default_factory=time.time)
    state: Optional[str] = None
    state_at: float = 0.0
    verifier: Optional[str] = None
    token: Optional[str] = None
    token_expires: float = 0.0
    docs: dict = field(default_factory=dict)       # uri -> listing entry, the only files this session may fetch

    @property
    def connected(self) -> bool:
        return bool(self.token) and time.time() < self.token_expires


_sessions: "OrderedDict[str, Session]" = OrderedDict()


def _sweep() -> None:
    now = time.time()
    for sid in [s for s, v in _sessions.items() if now - v.created > SESSION_TTL]:
        _sessions.pop(sid, None)
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)


def get_session(sid: Optional[str]) -> Optional[Session]:
    _sweep()
    return _sessions.get(sid) if sid else None


def new_session() -> tuple[str, Session]:
    _sweep()
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = Session()
    return sid, _sessions[sid]


def drop_session(sid: Optional[str]) -> None:
    if sid:
        _sessions.pop(sid, None)


# ---- OAuth: start and finish ---------------------------------------------------------
def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def begin(sess: Session, cfg: Settings) -> str:
    """Remember a one-time state and PKCE verifier; return the URL to send the browser to."""
    if not cfg.configured:
        raise DigiLockerError("DigiLocker is not set up on this server.")
    sess.state, sess.state_at = secrets.token_urlsafe(24), time.time()
    sess.verifier = secrets.token_urlsafe(64)
    if cfg.mock:
        return "/digilocker/callback?" + urlencode({"code": "mock-code", "state": sess.state})
    return cfg.base + PATH_AUTHORIZE + "?" + urlencode({
        "response_type": "code", "client_id": cfg.client_id, "redirect_uri": cfg.redirect_uri,
        "state": sess.state, "code_challenge": _challenge(sess.verifier), "code_challenge_method": "S256"})


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=_transport, follow_redirects=False)


async def finish(sess: Session, cfg: Settings, code: str, state: str) -> None:
    """Check the state, trade the code for an access token, keep it server-side."""
    ok = bool(sess.state) and secrets.compare_digest(sess.state.encode(), (state or "").encode()) \
        and time.time() - sess.state_at <= STATE_TTL
    verifier, sess.state, sess.verifier = sess.verifier, None, None      # one use only
    if not ok or not verifier:
        raise DigiLockerError("The sign-in expired or did not start here. Please try again.")
    if not code:
        raise DigiLockerError("DigiLocker did not return an authorization code.")
    if cfg.mock:
        sess.token, sess.token_expires = "mock-token", time.time() + 1800
        return
    try:
        async with _client() as c:
            r = await c.post(cfg.base + PATH_TOKEN, headers={"Accept": "application/json"}, data={
                "grant_type": "authorization_code", "code": code, "client_id": cfg.client_id,
                "client_secret": cfg.client_secret, "redirect_uri": cfg.redirect_uri, "code_verifier": verifier})
    except httpx.HTTPError:
        raise DigiLockerError("Could not reach DigiLocker. Try again in a moment.")
    if r.status_code != 200:
        raise DigiLockerError(f"DigiLocker did not accept the sign-in (HTTP {r.status_code}).")
    try:
        j = r.json()
        token, ttl = j["access_token"], int(j.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise DigiLockerError("DigiLocker sent an answer this server could not read.")
    sess.token, sess.token_expires = token, time.time() + max(60, min(ttl, 3600))


# ---- documents ------------------------------------------------------------------------
def _txt(v, n=200) -> str:
    return re.sub(r"[\x00-\x1f]", " ", str(v or ""))[:n]


def _is_pdf(item: dict) -> bool:
    mime = item.get("mime")
    mimes = mime if isinstance(mime, list) else [mime]
    return any(str(m).lower() == "application/pdf" for m in mimes)


def _mock_items() -> list[dict]:
    with open(os.path.join(SYN, "manifest.json"), encoding="utf-8") as fh:
        clean = next(s for s in json.load(fh) if s["id"] == "clean")
    return [{"name": dt.replace("_", " ").title(), "uri": "mock/" + dt, "type": "file", "mime": ["application/pdf"],
             "issuer": "Mock DigiLocker issuer", "doctype": dt, "date": "", "size": ""} for dt in clean["files"]]


async def list_issued(sess: Session, cfg: Settings) -> list[dict]:
    """The user's issued PDFs, cleaned up. Also remembers which URIs this session may fetch."""
    if not sess.connected:
        raise DigiLockerError("Connect DigiLocker first.")
    raw = _mock_items() if cfg.mock else await _real_listing(sess, cfg)
    items = []
    for it in raw:
        if not isinstance(it, dict) or it.get("type") == "dir" or not it.get("uri") or not _is_pdf(it):
            continue
        items.append({"uri": _txt(it["uri"], 300), "name": _txt(it.get("name")) or "Document",
                      "issuer": _txt(it.get("issuer")), "doctype": _txt(it.get("doctype"), 40),
                      "date": _txt(it.get("date"), 40), "size": _txt(it.get("size"), 20)})
        if len(items) >= MAX_LISTED:
            break
    sess.docs = {i["uri"]: i for i in items}
    return items


async def _real_listing(sess: Session, cfg: Settings) -> list:
    try:
        async with _client() as c:
            r = await c.get(cfg.base + PATH_ISSUED, headers={"Authorization": f"Bearer {sess.token}",
                                                             "Accept": "application/json"})
    except httpx.HTTPError:
        raise DigiLockerError("Could not reach DigiLocker. Try again in a moment.")
    if r.status_code in (401, 403):
        sess.token = None
        raise DigiLockerError("The DigiLocker connection expired. Connect again.")
    if r.status_code != 200:
        raise DigiLockerError(f"DigiLocker could not list documents (HTTP {r.status_code}).")
    try:
        items = r.json().get("items") or []
    except (ValueError, AttributeError):
        raise DigiLockerError("DigiLocker sent an answer this server could not read.")
    return items if isinstance(items, list) else []


async def fetch_pdf(sess: Session, cfg: Settings, uri: str, max_bytes: int) -> tuple[str, bytes]:
    """Download one listed PDF. Refuses anything not in this session's listing, not a PDF, or too big."""
    if not sess.connected:
        raise DigiLockerError("Connect DigiLocker first.")
    meta = sess.docs.get(uri)
    if meta is None:
        raise DigiLockerError("That document was not in your DigiLocker list. Refresh the list and try again.")
    if cfg.mock:
        dt = uri.split("/", 1)[1]
        with open(os.path.join(SYN, "clean", f"{dt}.pdf"), "rb") as fh:       # dt is whitelisted by sess.docs
            data = fh.read()
    else:
        data = bytearray()
        try:
            async with _client() as c:
                async with c.stream("GET", cfg.base + PATH_FILE + quote(uri, safe=""),
                                    headers={"Authorization": f"Bearer {sess.token}"}) as r:
                    if r.status_code in (401, 403):
                        sess.token = None
                        raise DigiLockerError("The DigiLocker connection expired. Connect again.")
                    if r.status_code != 200:
                        raise DigiLockerError(f"{meta['name']}: DigiLocker would not give the file (HTTP {r.status_code}).")
                    async for chunk in r.aiter_bytes():
                        data += chunk
                        if len(data) > max_bytes:
                            raise DigiLockerError(f"{meta['name']}: larger than {max_bytes // (1024 * 1024)} MB.")
        except httpx.HTTPError:
            raise DigiLockerError("Could not reach DigiLocker. Try again in a moment.")
        data = bytes(data)
    if not data.lstrip()[:5] == b"%PDF-":
        raise DigiLockerError(f"{meta['name']}: DigiLocker did not return a PDF.")
    fname = re.sub(r"[^A-Za-z0-9._-]+", "_", meta["name"]).strip("._") or "document"
    return f"digilocker_{fname}.pdf", data
