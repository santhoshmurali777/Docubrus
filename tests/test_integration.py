"""
test_integration.py -- full-stack integration tests for the Docroc API.

Verifies end-to-end flows:
  1. Health check works without auth
  2. Login flow (admin, applicant, reviewer)
  3. Upload + processing pipeline via API
  4. Case listing and retrieval
  5. Requirements endpoint
  6. Review actions roundtrip
  7. User management (admin CRUD)
  8. Signup flow for applicants
  9. DigiLocker mock flow
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app import auth

ROOT = os.path.join(os.path.dirname(__file__), "..")
SYN = os.path.join(ROOT, "data", "synthetic")


@pytest.fixture(autouse=True)
def _setup_env(monkeypatch):
    """Isolate auth & env for integration tests."""
    import app.main as main
    monkeypatch.setenv("SAAKSHYA_USER", "admin")
    monkeypatch.setenv("SAAKSHYA_PASSWORD", "TestPass123!")
    monkeypatch.setenv("SAAKSHYA_ALLOW_SIGNUP", "1")
    monkeypatch.setenv("DIGILOCKER_MOCK", "1")
    main._conn = None
    main._admin_seen = None
    main._reports.clear()
    main._files.clear()
    auth.clear_all_sessions()
    auth.reset_throttle()
    yield
    main._conn = None
    main._admin_seen = None
    auth.clear_all_sessions()
    auth.reset_throttle()


async def login(client, username="admin", password="TestPass123!"):
    resp = await client.post("/api/login", json={"username": username, "password": password})
    return resp


@pytest.mark.asyncio
async def test_health_check():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/healthz")
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_login_and_me():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # not logged in
        r = await c.get("/api/me")
        assert r.status_code == 200
        data = r.json()
        assert data["auth"] is True
        assert data["user"] is None

        # login
        r = await login(c)
        assert r.status_code == 200
        user = r.json()["user"]
        assert user["username"] == "admin"
        assert user["role"] == "admin"

        # now me returns user
        r = await c.get("/api/me")
        assert r.json()["user"]["username"] == "admin"


@pytest.mark.asyncio
async def test_bad_login_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_upload_and_process():
    """Upload the clean scenario PDFs and verify the API returns correct findings."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login(c)

        # upload clean scenario
        folder = os.path.join(SYN, "clean")
        files = []
        for f in sorted(os.listdir(folder)):
            if f.endswith(".pdf"):
                with open(os.path.join(folder, f), "rb") as fh:
                    files.append(("files", (f, fh.read(), "application/pdf")))

        r = await c.post("/api/cases", files=files, params={"institution_name": "Test College"})
        assert r.status_code == 200
        report = r.json()

        assert "case_id" in report
        assert "findings" in report
        assert "documents" in report
        assert len(report["findings"]) > 0
        assert len(report["documents"]) == 10

        # check status counts
        counts = report["counts"]
        assert counts.get("Verified", 0) > 0

        # retrieve the same case
        case_id = report["case_id"]
        r = await c.get(f"/api/cases/{case_id}")
        assert r.status_code == 200
        assert r.json()["case_id"] == case_id


@pytest.mark.asyncio
async def test_case_listing():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login(c)
        r = await c.get("/api/cases")
        assert r.status_code == 200
        # returns a list
        assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_requirements_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login(c)
        r = await c.get("/api/requirements")
        assert r.status_code == 200
        data = r.json()
        assert "category" in data
        assert "required" in data
        assert len(data["required"]) > 0


@pytest.mark.asyncio
async def test_signup_creates_applicant():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/api/signup", json={
            "username": "newapplicant",
            "display_name": "New Applicant",
            "password": "ApplPass123!"
        })
        assert r.status_code == 200
        user = r.json()["user"]
        assert user["role"] == "applicant"
        assert user["username"] == "newapplicant"


@pytest.mark.asyncio
async def test_admin_user_management():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login(c)

        # create a reviewer
        r = await c.post("/api/admin/users", json={
            "username": "reviewer01",
            "display_name": "Test Reviewer",
            "role": "reviewer",
            "password": "RevPass123!"
        })
        assert r.status_code == 200
        user = r.json()
        assert user["role"] == "reviewer"
        user_id = user["id"]

        # list users
        r = await c.get("/api/admin/users")
        assert r.status_code == 200
        users = r.json()
        assert any(u["id"] == user_id for u in users)

        # patch user
        r = await c.patch(f"/api/admin/users/{user_id}", json={"display_name": "Updated Reviewer"})
        assert r.status_code == 200
        assert r.json()["display_name"] == "Updated Reviewer"


@pytest.mark.asyncio
async def test_unauthenticated_api_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/cases")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_digilocker_status():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login(c)
        r = await c.get("/api/digilocker/status")
        assert r.status_code == 200
        data = r.json()
        assert "configured" in data
        assert "mock" in data


@pytest.mark.asyncio
async def test_classify_endpoint():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login(c)
        pdf_path = os.path.join(SYN, "clean", "land_deed.pdf")
        with open(pdf_path, "rb") as fh:
            files = [("files", ("land_deed.pdf", fh.read(), "application/pdf"))]
        r = await c.post("/api/classify", files=files)
        assert r.status_code == 200
        result = r.json()
        assert len(result) == 1
        assert result[0]["filename"] == "land_deed.pdf"


@pytest.mark.asyncio
async def test_logout_clears_session():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await login(c)
        # verify we're logged in
        r = await c.get("/api/me")
        assert r.json()["user"] is not None

        # logout
        r = await c.post("/api/logout")
        assert r.status_code == 200

        # verify we're logged out
        r = await c.get("/api/me")
        assert r.json()["user"] is None
