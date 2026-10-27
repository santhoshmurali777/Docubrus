"""Optional shared password: off by default, on when both env vars are set, /healthz always open."""
import base64
import warnings

from fastapi.testclient import TestClient

from app.main import app

warnings.filterwarnings("ignore")


def _basic(u, p):
    return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}


def test_open_when_no_password_is_configured(monkeypatch):
    monkeypatch.delenv("SAAKSHYA_USER", raising=False)
    monkeypatch.delenv("SAAKSHYA_PASSWORD", raising=False)
    assert TestClient(app).get("/api/scenarios").status_code == 200


def test_password_required_when_configured(monkeypatch):
    monkeypatch.setenv("SAAKSHYA_USER", "judge")
    monkeypatch.setenv("SAAKSHYA_PASSWORD", "s3cret")
    c = TestClient(app)
    r = c.get("/api/scenarios")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert c.get("/", headers=_basic("judge", "wrong")).status_code == 401
    assert c.get("/", headers=_basic("nobody", "s3cret")).status_code == 401
    assert c.get("/", headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
    assert c.get("/api/scenarios", headers=_basic("judge", "s3cret")).status_code == 200
    assert c.get("/", headers=_basic("judge", "s3cret")).status_code == 200


def test_health_check_stays_open(monkeypatch):
    monkeypatch.setenv("SAAKSHYA_USER", "judge")
    monkeypatch.setenv("SAAKSHYA_PASSWORD", "s3cret")
    assert TestClient(app).get("/healthz").status_code == 200
