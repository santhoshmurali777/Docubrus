import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
"""DigiLocker connect: OAuth state/PKCE, session-bound file whitelist, and import into a case."""
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import digilocker
from tests.test_demo import SYN


@pytest.fixture
def client(monkeypatch):
    main._conn = None; main._reports.clear(); main._files.clear(); digilocker._sessions.clear()
    for k in ("DIGILOCKER_CLIENT_ID", "DIGILOCKER_CLIENT_SECRET", "DIGILOCKER_REDIRECT_URI", "DIGILOCKER_MOCK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(digilocker, "_transport", None)
    return TestClient(main.app)


def connect_mock(c, monkeypatch):
    monkeypatch.setenv("DIGILOCKER_MOCK", "1")
    r = c.get("/digilocker/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/digilocker/callback")
    r = c.get(r.headers["location"], follow_redirects=False)
    assert r.headers["location"] == "/?dl=connected"


def test_not_configured_by_default(client):
    assert client.get("/api/digilocker/status").json() == {"configured": False, "mock": False, "connected": False}
    assert client.get("/digilocker/login", follow_redirects=False).headers["location"] == "/?dl=unconfigured"
    assert client.get("/api/digilocker/documents").status_code == 401


def test_mock_flow_imports_a_full_case(client, monkeypatch):
    connect_mock(client, monkeypatch)
    assert client.get("/api/digilocker/status").json()["connected"] is True
    docs = client.get("/api/digilocker/documents").json()["documents"]
    assert len(docs) == 10
    r = client.post("/api/digilocker/import", json={"uris": [d["uri"] for d in docs], "institution_name": "Test Trust"})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["institution"] == "Test Trust" and len(rep["documents"]) == 10
    assert client.get(f"/api/cases/{rep['case_id']}").status_code == 200
    # same engine as an upload: the clean sample has no missing documents
    assert rep["missing_documents"] == []


def test_session_cookie_is_httponly_and_token_not_exposed(client, monkeypatch):
    monkeypatch.setenv("DIGILOCKER_MOCK", "1")
    r = client.get("/digilocker/login", follow_redirects=False)
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    client.get(r.headers["location"], follow_redirects=False)
    assert "mock-token" not in client.get("/api/digilocker/status").text
    assert "mock-token" not in client.get("/api/digilocker/documents").text


def test_wrong_state_rejected_and_state_is_single_use(client, monkeypatch):
    monkeypatch.setenv("DIGILOCKER_MOCK", "1")
    r = client.get("/digilocker/login", follow_redirects=False)
    bad = client.get("/digilocker/callback?code=x&state=nope", follow_redirects=False)
    assert bad.headers["location"] == "/?dl=failed"
    assert client.get("/api/digilocker/status").json()["connected"] is False
    # the genuine state was burned by the failed attempt
    again = client.get(r.headers["location"], follow_redirects=False)
    assert again.headers["location"] == "/?dl=failed"


def test_callback_without_session_and_user_denial(client, monkeypatch):
    monkeypatch.setenv("DIGILOCKER_MOCK", "1")
    assert client.get("/digilocker/callback?code=x&state=y", follow_redirects=False).headers["location"] == "/?dl=expired"
    client.get("/digilocker/login", follow_redirects=False)
    r = client.get("/digilocker/callback?error=access_denied", follow_redirects=False)
    assert r.headers["location"] == "/?dl=denied"


def test_cannot_fetch_a_file_that_was_not_listed(client, monkeypatch):
    connect_mock(client, monkeypatch)
    client.get("/api/digilocker/documents")
    for uri in ("mock/../../etc/passwd", "mock/fire_noc_extra", "not-listed"):
        r = client.post("/api/digilocker/import", json={"uris": [uri]})
        assert r.status_code == 502 and "not in your DigiLocker list" in r.json()["detail"]


def test_import_before_listing_is_refused(client, monkeypatch):
    connect_mock(client, monkeypatch)
    r = client.post("/api/digilocker/import", json={"uris": ["mock/fire_noc"]})
    assert r.status_code == 502


def test_disconnect_ends_the_session(client, monkeypatch):
    connect_mock(client, monkeypatch)
    client.post("/api/digilocker/disconnect")
    assert client.get("/api/digilocker/status").json()["connected"] is False
    assert client.get("/api/digilocker/documents").status_code == 401


def test_too_many_uris_rejected(client, monkeypatch):
    connect_mock(client, monkeypatch)
    assert client.post("/api/digilocker/import", json={"uris": [f"u{i}" for i in range(16)]}).status_code == 422


# ---- real mode, against a fake DigiLocker server ----------------------------------------
PDF = open(os.path.join(SYN, "clean", "fire_noc.pdf"), "rb").read()


def fake_digilocker(seen):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/public/oauth2/1/token":
            seen["token_form"] = parse_qs(req.content.decode())
            return httpx.Response(200, json={"access_token": "real-token", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer real-token"
        if req.url.path == "/public/oauth2/1/files/issued":
            return httpx.Response(200, json={"items": [
                {"name": "Fire NOC", "uri": "in.gov.fire-NOC-123", "type": "file", "mime": ["application/pdf"], "issuer": "Fire Dept"},
                {"name": "A folder", "uri": "dir1", "type": "dir", "mime": []},
                {"name": "Aadhaar XML", "uri": "x1", "type": "file", "mime": ["application/xml"]}]})
        if req.url.path.startswith("/public/oauth2/1/file/"):
            seen["file_path"] = req.url.raw_path.decode()
            return httpx.Response(200, content=PDF if "123" in req.url.path else b"<html>nope</html>")
        return httpx.Response(404)
    return handler


def test_real_mode_uses_pkce_and_exchanges_code(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(digilocker, "_transport", httpx.MockTransport(fake_digilocker(seen)))
    monkeypatch.setenv("DIGILOCKER_CLIENT_ID", "cid")
    monkeypatch.setenv("DIGILOCKER_CLIENT_SECRET", "sec")
    monkeypatch.setenv("DIGILOCKER_REDIRECT_URI", "https://site.example/digilocker/callback")
    r = client.get("/digilocker/login", follow_redirects=False)
    u = urlparse(r.headers["location"]); q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.netloc == "api.digitallocker.gov.in" and u.path == "/public/oauth2/1/authorize"
    assert q["response_type"] == "code" and q["client_id"] == "cid" and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://site.example/digilocker/callback"
    cb = client.get("/digilocker/callback", params={"code": "abc", "state": q["state"]}, follow_redirects=False)
    assert cb.headers["location"] == "/?dl=connected"
    form = {k: v[0] for k, v in seen["token_form"].items()}
    assert form["grant_type"] == "authorization_code" and form["code"] == "abc" and form["client_secret"] == "sec"
    assert base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    docs = client.get("/api/digilocker/documents").json()["documents"]
    assert [d["uri"] for d in docs] == ["in.gov.fire-NOC-123"]           # folder and non-PDF filtered out
    r = client.post("/api/digilocker/import", json={"uris": ["in.gov.fire-NOC-123"]})
    assert r.status_code == 200, r.text
    assert r.json()["documents"][0]["filename"].startswith("digilocker_Fire_NOC")


def test_real_mode_rejects_non_pdf_and_token_failure(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(digilocker, "_transport", httpx.MockTransport(fake_digilocker(seen)))
    for k, v in (("DIGILOCKER_CLIENT_ID", "cid"), ("DIGILOCKER_CLIENT_SECRET", "sec"), ("DIGILOCKER_REDIRECT_URI", "https://s/cb")):
        monkeypatch.setenv(k, v)
    state = parse_qs(urlparse(client.get("/digilocker/login", follow_redirects=False).headers["location"]).query)["state"][0]
    client.get("/digilocker/callback", params={"code": "abc", "state": state})
    sess = next(iter(digilocker._sessions.values()))
    sess.docs = {"bad-uri": {"name": "Odd", "uri": "bad-uri"}}
    r = client.post("/api/digilocker/import", json={"uris": ["bad-uri"]})
    assert r.status_code == 502 and "did not return a PDF" in r.json()["detail"]

    # DigiLocker refuses the code
    monkeypatch.setattr(digilocker, "_transport", httpx.MockTransport(lambda req: httpx.Response(400, json={"error": "invalid_grant"})))
    state = parse_qs(urlparse(client.get("/digilocker/login", follow_redirects=False).headers["location"]).query)["state"][0]
    cb = client.get("/digilocker/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert cb.headers["location"] == "/?dl=failed"
