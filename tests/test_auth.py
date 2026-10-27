"""Sign-in: off by default (local mode), on when the first admin is configured, /healthz always open."""
import warnings

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import auth

warnings.filterwarnings("ignore")

ADMIN, ADMIN_PW = "judge", "s3cret-pass"


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setenv("SAAKSHYA_USER", ADMIN)
    monkeypatch.setenv("SAAKSHYA_PASSWORD", ADMIN_PW)
    monkeypatch.delenv("SAAKSHYA_ALLOW_SIGNUP", raising=False)
    monkeypatch.delenv("SAAKSHYA_DB", raising=False)
    main._conn = None; main._admin_seen = None; main._reports.clear(); main._files.clear()
    auth.clear_all_sessions(); auth.reset_throttle()
    yield
    main._conn = None; main._admin_seen = None


def signed_in(username, password):
    c = TestClient(main.app)
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_open_when_no_password_is_configured(monkeypatch):
    monkeypatch.delenv("SAAKSHYA_USER", raising=False)
    monkeypatch.delenv("SAAKSHYA_PASSWORD", raising=False)
    c = TestClient(main.app)
    assert c.get("/api/scenarios").status_code == 200
    me = c.get("/api/me").json()
    assert me["auth"] is False and me["user"]["local"] is True and me["user"]["role"] == "admin"
    assert c.get("/login", follow_redirects=False).status_code == 303       # nothing to sign in to


def test_sign_in_required_when_configured(secured):
    c = TestClient(main.app)
    r = c.get("/api/scenarios")
    assert r.status_code == 401 and r.json()["detail"] == "Sign in required."
    assert c.get("/api/cases").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/api/me").json()["user"] is None


def test_health_check_stays_open(secured):
    assert TestClient(main.app).get("/healthz").status_code == 200


def test_login_checks_username_and_password(secured):
    c = TestClient(main.app)
    for u, p in [(ADMIN, "wrong"), ("nobody", ADMIN_PW), ("", "")]:
        assert c.post("/api/login", json={"username": u, "password": p}).status_code == 401
    r = c.post("/api/login", json={"username": "  JUDGE ", "password": ADMIN_PW})      # names are case-insensitive
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert c.get("/api/scenarios").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200
    c.post("/api/logout")
    assert c.get("/api/scenarios").status_code == 401


def test_repeated_failures_are_throttled(secured):
    c = TestClient(main.app)
    for _ in range(auth.LIMIT_USER):
        assert c.post("/api/login", json={"username": ADMIN, "password": "bad"}).status_code == 401
    r = c.post("/api/login", json={"username": ADMIN, "password": ADMIN_PW})
    assert r.status_code == 429 and r.headers["retry-after"]


def test_cross_site_posts_are_refused(secured):
    c = signed_in(ADMIN, ADMIN_PW)
    r = c.post("/api/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.get("/api/scenarios").status_code == 200          # still signed in


def test_changing_the_env_password_recovers_the_admin(secured, monkeypatch):
    signed_in(ADMIN, ADMIN_PW)
    monkeypatch.setenv("SAAKSHYA_PASSWORD", "another-pass-1")
    assert TestClient(main.app).post("/api/login", json={"username": ADMIN, "password": ADMIN_PW}).status_code == 401
    assert signed_in(ADMIN, "another-pass-1").get("/api/me").json()["user"]["role"] == "admin"
