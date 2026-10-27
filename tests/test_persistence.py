import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
"""W7: cases survive a restart, and slow analyses never block the health check."""
import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main


def reset():
    main._conn = None; main._reports.clear(); main._files.clear(); main._queued = 0


@pytest.fixture
def filedb(tmp_path, monkeypatch):
    monkeypatch.setenv("SAAKSHYA_DB", str(tmp_path / "t.db"))
    reset()
    yield
    reset()


def restart():
    """What a process restart does to this app: memory is gone, the file stays."""
    if main._conn is not None:
        main._DB.submit(main._conn.close).result()
    reset()


def test_case_pages_and_reviews_survive_a_restart(filedb):
    c = TestClient(main.app)
    rep = c.post("/api/demo/forged_qr").json()
    cid, doc = rep["case_id"], rep["documents"][0]["id"]
    fid = rep["findings"][0]["finding_id"]
    assert c.post(f"/api/cases/{cid}/actions", json={"kind": "comment", "actor": "Asha", "note": "looks odd", "finding_id": fid}).status_code == 200
    restart()
    c = TestClient(main.app)
    assert c.get(f"/api/cases/{cid}").json()["case_id"] == cid
    assert [r["case_id"] for r in c.get("/api/cases").json()] == [cid]
    assert c.get(f"/api/cases/{cid}/documents/{doc}/pages/1").status_code == 200       # original PDF came back from the file
    acts = c.get(f"/api/cases/{cid}/review").json()["actions"]
    assert [a["note"] for a in acts] == ["looks odd"]


def test_unknown_case_is_404_in_file_mode(filedb):
    assert TestClient(main.app).get("/api/cases/case_nope").status_code == 404


def test_only_the_newest_cases_are_kept(filedb, monkeypatch):
    monkeypatch.setattr(main, "MAX_REPORTS", 2)
    c = TestClient(main.app)
    ids = [c.post(f"/api/demo/{s}").json()["case_id"] for s in ("clean", "forged_qr", "survey_mismatch")]
    restart()
    c = TestClient(main.app)
    kept = {r["case_id"] for r in c.get("/api/cases").json()}
    assert kept == set(ids[1:])
    assert c.get(f"/api/cases/{ids[0]}").status_code == 404


def test_memory_mode_forgets_after_restart(monkeypatch):
    monkeypatch.delenv("SAAKSHYA_DB", raising=False)
    reset()
    c = TestClient(main.app)
    cid = c.post("/api/demo/clean").json()["case_id"]
    restart()
    assert TestClient(main.app).get(f"/api/cases/{cid}").status_code == 404


# ---- the server stays responsive -----------------------------------------------------------
def slow(monkeypatch, seconds):
    real = main.analyze

    def slower(*a, **k):
        time.sleep(seconds)
        return real(*a, **k)
    monkeypatch.setattr(main, "analyze", slower)


def test_health_check_answers_while_analyses_run(monkeypatch):
    monkeypatch.delenv("SAAKSHYA_DB", raising=False)
    reset(); slow(monkeypatch, 1.0)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t", timeout=60) as ac:
            jobs = [asyncio.create_task(ac.post("/api/demo/clean")) for _ in range(4)]
            await asyncio.sleep(0.3)
            t = time.perf_counter(); r = await ac.get("/healthz"); took = time.perf_counter() - t
            assert r.status_code == 200 and took < 0.5, took
            assert [x.status_code for x in await asyncio.gather(*jobs)] == [200] * 4
    asyncio.run(go())


def test_too_many_waiting_analyses_get_503(monkeypatch):
    monkeypatch.delenv("SAAKSHYA_DB", raising=False)
    reset(); slow(monkeypatch, 0.8); monkeypatch.setattr(main, "MAX_QUEUE", 2)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t", timeout=60) as ac:
            codes = sorted(x.status_code for x in await asyncio.gather(*[ac.post("/api/demo/clean") for _ in range(5)]))
            assert codes == [200, 200, 503, 503, 503], codes
            assert (await ac.get("/healthz")).status_code == 200
    asyncio.run(go())
    assert main._queued == 0                                  # the counter is released even for refused/finished jobs


def test_oversized_pdf_is_refused_not_read(monkeypatch):
    from app.documents import pdf_text
    with open(os.path.join(os.path.dirname(__file__), "..", "data", "synthetic", "clean", "fire_noc.pdf"), "rb") as fh:
        data = fh.read()
    import pdfplumber, io
    n = len(pdfplumber.open(io.BytesIO(data)).pages)
    monkeypatch.setattr(pdf_text, "MAX_PAGES", max(0, n - 1))
    with pytest.raises(ValueError, match="more than"):
        pdf_text.read_pdf(data)
