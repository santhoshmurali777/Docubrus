"""
test_suite_v2.py -- Verification for the new Docroc platform:
  1. Role-checked login:
     - Unknown user -> generic 401 error
     - Correct username + wrong password -> same generic 401 error
     - Correct username + password + WRONG ROLE -> SAME generic 401 error
     - Matching role + credentials -> 200 OK
  2. 5-attempt rate-limiting lockout.
  3. Protected last administrator (cannot delete, demote, or disable).
  4. Customise config model, sanitized applicant checklist, presets.
  5. "Papers, Please" cross-document discrepancy engine:
     - Names: word order variation, initials, honorific stripping
     - Dates: formats, day/month swaps
     - Addresses: PIN code checks and token overlap
     - Masked IDs: visible trailing digit comparison
     - Reused file detection via SHA-256
  6. Data delivery modes:
     - 'data_only' wipes PDFs and stores only structured data
     - 'deliver_files' stores PDFs
"""
from __future__ import annotations

import datetime
import hashlib
import os
import pytest
from fastapi.testclient import TestClient

from app import auth, db, engine
from app.config_model import (
    CustomiseConfig,
    PRESET_COLLEGE,
    PRESET_GOVERNMENT,
    PRESET_BLANK,
    PRESETS,
)
from app.main import app, _conn


@pytest.fixture(autouse=True)
def clean_state():
    auth.clear_all_sessions()
    auth.reset_throttle()
    db.init_db(_conn)
    yield
    auth.clear_all_sessions()
    auth.reset_throttle()


# ---- 1. Role-Checked Login -------------------------------------------------------------------------------

def test_role_checked_login():
    with TestClient(app, raise_server_exceptions=False) as client:
        # Unknown user
        r = client.post("/api/login", json={"username": "unknown_user", "password": "anypassword", "role": "applicant"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid username, password, or role."

        # Correct username + wrong password -> same error
        r = client.post("/api/login", json={"username": "admin", "password": "WrongPassword!", "role": "admin"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid username, password, or role."

        # Correct username + correct password + WRONG ROLE -> SAME GENERIC ERROR
        r = client.post("/api/login", json={"username": "admin", "password": "Admin123!", "role": "applicant"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid username, password, or role."

        # Correct username + correct password + MATCHING ROLE -> SUCCESS
        r = client.post("/api/login", json={"username": "admin", "password": "Admin123!", "role": "admin"})
        assert r.status_code == 200
        assert r.json()["user"]["username"] == "admin"
        assert r.json()["user"]["role"] == "admin"


def test_lockout_after_five_failed_attempts():
    with TestClient(app, raise_server_exceptions=False) as client:
        for _ in range(5):
            r = client.post("/api/login", json={"username": "admin", "password": "bad", "role": "admin"})
            assert r.status_code == 401

        # 6th attempt is locked out
        r = client.post("/api/login", json={"username": "admin", "password": "Admin123!", "role": "admin"})
        assert r.status_code == 429
        assert "locked" in r.json()["detail"].lower()


# ---- 2. Protected Last Administrator ---------------------------------------------------------------------

def test_protected_last_admin():
    admin = db.get_user_by_username(_conn, "admin")
    assert admin is not None

    # Demotion blocked
    ok, msg = db.update_user(_conn, admin["id"], role="applicant")
    assert not ok
    assert "last active administrator" in msg.lower()

    # Disabling blocked
    ok, msg = db.update_user(_conn, admin["id"], disabled=True)
    assert not ok
    assert "last active administrator" in msg.lower()

    # Deletion blocked
    ok, msg = db.delete_user(_conn, admin["id"])
    assert not ok
    assert "last active administrator" in msg.lower()


# ---- 3. Customise Model, Presets, and Sanitized Checklist ------------------------------------------------

def test_sanitized_applicant_checklist():
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        assert "documents" in data
        assert "cross_checks" not in data       # Hidden from applicants
        assert "rules" not in data              # Hidden from applicants
        assert len(data["documents"]) > 0


def test_presets_loading_and_switch():
    with TestClient(app, raise_server_exceptions=False) as client:
        # Sign in as admin
        client.post("/api/login", json={"username": "admin", "password": "Admin123!", "role": "admin"})

        # Get full config
        r = client.get("/api/config")
        assert r.status_code == 200
        assert "College" in r.json()["available_presets"]
        assert "Government Office" in r.json()["available_presets"]

        # Switch to Government Office preset
        r = client.post("/api/config", json={"preset_name": "Government Office"})
        assert r.status_code == 200
        assert r.json()["config"]["preset_name"] == "Government Office"

        # Switch back to College
        client.post("/api/config", json={"preset_name": "College"})


# ---- 4. "Papers, Please" Discrepancy Matching ------------------------------------------------------------

def test_name_matching():
    ok, _ = engine.match_names("Rajesh Sharma", "Rajesh Sharma")
    assert ok
    ok, _ = engine.match_names("Sharma Rajesh", "Rajesh Sharma")
    assert ok
    ok, _ = engine.match_names("Mr. Rajesh Sharma", "Shri Rajesh Sharma")
    assert ok
    ok, _ = engine.match_names("R. Sharma", "Rajesh Sharma")
    assert ok
    ok, _ = engine.match_names("Rajesh Sharma", "Pooja Sharma")
    assert not ok


def test_date_matching():
    ok, _ = engine.match_dates("2004-05-16", "16/05/2004")
    assert ok
    ok, msg = engine.match_dates("2004-05-06", "2004-06-05")
    assert not ok
    assert "swapped" in msg.lower()
    ok, _ = engine.match_dates("2004-05-16", "2001-01-01")
    assert not ok


def test_address_matching():
    a1 = "Flat 102, Orchid Apartments, MG Road, Bangalore 560001"
    a2 = "102 Orchid Apts, Mahatma Gandhi Road, Bangalore 560001"
    ok, _ = engine.match_addresses(a1, a2)
    assert ok

    # PIN code mismatch
    a3 = "Flat 102, Orchid Apartments, MG Road, Mumbai 400001"
    ok, msg = engine.match_addresses(a1, a3)
    assert not ok
    assert "pin" in msg.lower()


def test_masked_id_matching():
    ok, _ = engine.match_masked_ids("XXXX-XXXX-9842", "1234-5678-9842")
    assert ok
    ok, _ = engine.match_masked_ids("XXXX-XXXX-9842", "1234-5678-1111")
    assert not ok


# ---- 5. Reused Document & Delivery Modes -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_reused_file_detection():
    config = PRESET_COLLEGE
    pdf_sample = b"%PDF-1.4\n1 0 obj\n<< /Title (Doc) >>\nendobj\ntrailer\n<< >>\n%%EOF"
    uploads = {
        "marksheet_10": ("cert10.pdf", pdf_sample),
        "marksheet_12": ("cert12.pdf", pdf_sample),
    }
    report = await engine.run_processing_pipeline(config, uploads)
    assert report["verdict"] == "Not cleared"
    assert any("exact same file" in r.lower() for r in report["verdict_reasons"])


def test_data_only_delivery_mode_wipes_files():
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/api/login", json={"username": "admin", "password": "Admin123!", "role": "admin"})

        cfg = db.get_customise_config(_conn)
        cfg.delivery_mode = "data_only"
        db.save_customise_config(_conn, cfg)

        pdf_sample = b"%PDF-1.4\n1 0 obj\n<< /Title (ID) >>\nendobj\ntrailer\n<< >>\n%%EOF"
        files = [("files", ("identity_card.pdf", pdf_sample, "application/pdf"))]

        r = client.post("/api/submissions", files=files)
        assert r.status_code == 200
        sub_id = r.json()["id"]

        sub = db.get_submission(_conn, sub_id)
        assert sub["files_wiped"] == 1
        assert db.get_submission_file(_conn, sub_id, "identity_card") is None

        # Reset
        cfg.delivery_mode = "deliver_files"
        db.save_customise_config(_conn, cfg)
