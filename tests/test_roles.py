"""Applicant / reviewer / admin: who may do what, and who sees which case."""
import os
import warnings

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import auth, db
from tests.helpers import ROOT
from tests.test_auth import ADMIN, ADMIN_PW, secured, signed_in    # noqa: F401  (secured is a fixture)

warnings.filterwarnings("ignore")
CLEAN = os.path.join(ROOT, "data", "synthetic", "clean")


def make(admin, username, role, password="pass-word-1", name=""):
    r = admin.post("/api/admin/users", json={"username": username, "display_name": name, "role": role, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def upload(c, name="Trust A"):
    files = [("files", (f, open(os.path.join(CLEAN, f), "rb"), "application/pdf")) for f in sorted(os.listdir(CLEAN))]
    try:
        r = c.post("/api/cases?institution_name=" + name, files=files)
    finally:
        for _, (_, fh, _) in files:
            fh.close()
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def world(secured):
    admin = signed_in(ADMIN, ADMIN_PW)
    make(admin, "asha", "applicant", name="Asha"); make(admin, "ben", "applicant", name="Ben")
    make(admin, "rita", "reviewer", name="Rita R")
    return {"admin": admin, "asha": signed_in("asha", "pass-word-1"), "ben": signed_in("ben", "pass-word-1"),
            "rita": signed_in("rita", "pass-word-1")}


# ---- account management ---------------------------------------------------------------------------------------
def test_only_an_admin_manages_accounts(world):
    for who in ("asha", "rita"):
        assert world[who].get("/api/admin/users").status_code == 403
        assert world[who].post("/api/admin/users", json={"username": "x-user", "role": "admin", "password": "pass-word-1"}).status_code == 403
    users = {u["username"]: u for u in world["admin"].get("/api/admin/users").json()}
    assert users[ADMIN]["role"] == "admin" and users["asha"]["role"] == "applicant" and "password_hash" not in users["asha"]


def test_account_creation_rules(world):
    a = world["admin"]
    assert a.post("/api/admin/users", json={"username": "ASHA", "role": "applicant", "password": "pass-word-1"}).status_code == 409
    assert a.post("/api/admin/users", json={"username": "ok-name", "role": "applicant", "password": "short"}).status_code == 400
    assert a.post("/api/admin/users", json={"username": "no spaces", "role": "applicant", "password": "pass-word-1"}).status_code == 400
    assert a.post("/api/admin/users", json={"username": "ok-name", "role": "superuser", "password": "pass-word-1"}).status_code == 422


def test_disabling_signs_the_user_out_at_once(world):
    uid = next(u["id"] for u in world["admin"].get("/api/admin/users").json() if u["username"] == "ben")
    assert world["ben"].get("/api/cases").status_code == 200
    assert world["admin"].patch(f"/api/admin/users/{uid}", json={"disabled": True}).status_code == 200
    assert world["ben"].get("/api/cases").status_code == 401
    assert TestClient(main.app).post("/api/login", json={"username": "ben", "password": "pass-word-1"}).status_code == 401
    world["admin"].patch(f"/api/admin/users/{uid}", json={"disabled": False})
    signed_in("ben", "pass-word-1")


def test_role_change_applies_to_the_live_session(world):
    uid = next(u["id"] for u in world["admin"].get("/api/admin/users").json() if u["username"] == "ben")
    assert world["ben"].get("/api/scenarios").status_code == 403
    world["admin"].patch(f"/api/admin/users/{uid}", json={"role": "reviewer"})
    assert world["ben"].get("/api/scenarios").status_code == 200


def test_the_last_admin_cannot_be_removed(world):
    a = world["admin"]
    me = a.get("/api/me").json()["user"]["id"]
    assert a.patch(f"/api/admin/users/{me}", json={"role": "reviewer"}).status_code == 409
    assert a.patch(f"/api/admin/users/{me}", json={"disabled": True}).status_code == 409
    second = make(a, "boss2", "admin")
    assert a.patch(f"/api/admin/users/{second['id']}", json={"disabled": True}).status_code == 200    # one admin still remains
    assert a.patch(f"/api/admin/users/{me}", json={"disabled": True}).status_code == 409


def test_password_change_and_reset(world):
    asha = world["asha"]
    assert asha.post("/api/me/password", json={"current": "wrong-wrong", "new": "brand-new-pw"}).status_code == 403
    assert asha.post("/api/me/password", json={"current": "pass-word-1", "new": "tiny"}).status_code == 400
    other_device = signed_in("asha", "pass-word-1")
    assert asha.post("/api/me/password", json={"current": "pass-word-1", "new": "brand-new-pw"}).status_code == 200
    assert asha.get("/api/cases").status_code == 200               # this device stays signed in
    assert other_device.get("/api/cases").status_code == 401       # the others do not
    assert TestClient(main.app).post("/api/login", json={"username": "asha", "password": "pass-word-1"}).status_code == 401
    signed_in("asha", "brand-new-pw")
    uid = next(u["id"] for u in world["admin"].get("/api/admin/users").json() if u["username"] == "asha")
    world["admin"].patch(f"/api/admin/users/{uid}", json={"password": "reset-by-admin"})
    assert asha.get("/api/cases").status_code == 401
    signed_in("asha", "reset-by-admin")


def test_signup_is_closed_unless_switched_on(world, monkeypatch):
    c = TestClient(main.app)
    assert c.get("/api/me").json()["signup"] is False
    assert c.post("/api/signup", json={"username": "newbie", "password": "pass-word-1"}).status_code == 403
    monkeypatch.setenv("SAAKSHYA_ALLOW_SIGNUP", "1")
    assert c.get("/api/me").json()["signup"] is True
    r = c.post("/api/signup", json={"username": "newbie", "display_name": "New B", "password": "pass-word-1", "role": "admin"})
    assert r.status_code == 200 and r.json()["user"]["role"] == "applicant"        # a role in the body is ignored
    assert c.get("/api/me").json()["user"]["username"] == "newbie"
    assert c.get("/api/admin/users").status_code == 403


# ---- who sees which case ---------------------------------------------------------------------------------------
def test_an_applicant_sees_only_their_own_cases(world):
    case = upload(world["asha"], "Asha Trust")
    cid, doc = case["case_id"], case["documents"][0]["id"]
    assert [r["case_id"] for r in world["asha"].get("/api/cases").json()] == [cid]
    assert world["ben"].get("/api/cases").json() == []
    for url in (f"/api/cases/{cid}", f"/api/cases/{cid}/review", f"/api/cases/{cid}/documents/{doc}/pages/1"):
        assert world["asha"].get(url).status_code == 200, url
        assert world["ben"].get(url).status_code == 404, url          # indistinguishable from "no such case"
    for who in ("rita", "admin"):
        assert [r["case_id"] for r in world[who].get("/api/cases").json()] == [cid]
        assert world[who].get(f"/api/cases/{cid}").status_code == 200


def test_applicants_cannot_use_reviewer_tools(world):
    cid = upload(world["asha"])["case_id"]
    assert world["asha"].get("/api/scenarios").status_code == 403
    assert world["asha"].post("/api/demo/clean").status_code == 403
    r = world["asha"].post(f"/api/cases/{cid}/actions", json={"kind": "approve", "note": "me"})
    assert r.status_code == 403
    assert world["rita"].post("/api/demo/clean").status_code == 200


def test_the_reviewer_is_named_by_their_account_not_by_the_page(world):
    cid = upload(world["asha"])["case_id"]
    r = world["rita"].post(f"/api/cases/{cid}/actions", json={"kind": "comment", "actor": "Somebody Else", "note": "looks fine"})
    assert r.status_code == 200
    assert r.json()["action"]["actor"] == "Rita R (rita)"
    # an unsigned-in or foreign case id changes nothing
    assert world["rita"].post("/api/cases/nope/actions", json={"kind": "comment", "note": "x"}).status_code == 404


def test_an_applicant_sees_questions_and_decisions_but_not_internal_notes(world):
    cid = upload(world["asha"])["case_id"]
    rita = world["rita"]
    rita.post(f"/api/cases/{cid}/actions", json={"kind": "comment", "note": "internal: check the deed"})
    rita.post(f"/api/cases/{cid}/actions", json={"kind": "clarify", "note": "Please resend the fire NOC."})
    seen = world["asha"].get(f"/api/cases/{cid}/review").json()
    assert [a["kind"] for a in seen["actions"]] == ["clarify"]
    assert seen["state"]["case"] == "clarification_requested" and seen["state"]["open_clarifications"] == 1
    assert all("comments" not in v and "attested" not in v for v in seen["state"]["by_finding"].values())
    full = rita.get(f"/api/cases/{cid}/review").json()
    assert [a["kind"] for a in full["actions"]] == ["comment", "clarify"]


def test_cases_survive_with_their_owner_after_a_restart(world, tmp_path, monkeypatch):
    monkeypatch.setenv("SAAKSHYA_DB", str(tmp_path / "t.db"))
    main._conn = None; main._admin_seen = None; main._reports.clear(); main._files.clear(); auth.clear_all_sessions()
    admin = signed_in(ADMIN, ADMIN_PW)
    make(admin, "asha", "applicant"); make(admin, "ben", "applicant")
    asha, ben = signed_in("asha", "pass-word-1"), signed_in("ben", "pass-word-1")
    cid = upload(asha)["case_id"]
    main._conn.close(); main._conn = None; main._admin_seen = None; main._reports.clear(); main._files.clear()      # "restart"
    assert asha.get(f"/api/cases/{cid}").status_code == 200
    assert ben.get(f"/api/cases/{cid}").status_code == 404
    assert {u["username"] for u in admin.get("/api/admin/users").json()} == {ADMIN, "asha", "ben"}
    main._conn.close(); main._conn = None
