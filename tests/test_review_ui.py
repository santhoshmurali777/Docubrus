"""W5 in a real browser: viewer highlights, reviewer actions, triage list, phone layout.
Starts the server on a free port and drives Chromium; skipped when Playwright or its browser is missing."""
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

pw = pytest.importorskip("playwright.sync_api")
from tests.helpers import ROOT  # noqa: E402


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base():
    port = _free_port()
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, "SAAKSHYA_DB": ":memory:"})
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(url + "/healthz", timeout=1); break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill(); pytest.skip("server did not start")
    yield url
    proc.terminate(); proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:
            pytest.skip(f"no browser: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(viewport={"width": 1360, "height": 860})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    yield pg
    pg.close()


def open_sample(page, base, title):
    page.goto(base + "/#/")
    page.wait_for_selector("#samples button", state="attached")
    page.wait_for_selector("#samples button", state="visible")
    page.click(f'#samples button:text-is("{title}")')
    page.wait_for_selector(".case .bx.sel")


INTAKE = "Intake differs from regulator record"


def test_worst_finding_is_selected_and_drawn_on_the_page(page, base):
    open_sample(page, base, INTAKE)
    assert page.locator(".bx").count() >= 5
    assert "Contradicted" in page.inner_text(".bx.sel .chip")
    assert page.locator(".fi.sel").count() == 1
    page.wait_for_function("(() => { const i = document.querySelector('#sheet img'); return i && i.complete && i.naturalWidth > 100 })()")
    assert not page.errors


def test_finding_and_box_select_each_other(page, base):
    open_sample(page, base, INTAKE)
    first = page.locator(".fi.sel").get_attribute("id")
    other = page.locator(".fi .show:visible").last
    other.click()
    assert page.locator(".fi.sel").get_attribute("id") != first
    chip = page.inner_text(".bx.sel .chip")
    page.locator(".bx:not(.sel)").first.click()
    assert page.inner_text(".bx.sel .chip") != chip
    assert page.locator(".fi.sel").count() == 1


def test_reviewer_flow_name_clarify_resolve_approve(page, base):
    open_sample(page, base, INTAKE)
    row = page.locator(".fi.sel")
    row.locator("details.act summary").click()
    row.locator("textarea").fill("Please send the AICTE letter")
    row.locator(".btns button").nth(1).click()                              # clarify, but no name yet
    assert "name" in row.locator(".e").inner_text().lower()
    page.fill("#who", "Asha Rao")
    row.locator(".btns button").nth(1).click()
    page.wait_for_selector(".fi.sel .ai")
    assert "Waiting on institution" in page.inner_text("#rvState")
    page.click(".panel details.act summary")
    page.fill(".panel textarea", "approve")
    page.click('.panel .btns button:has-text("Approve case")')
    page.wait_for_function("document.querySelector('.panel .e').textContent.includes('open')")
    row.locator(".ai button").click()
    page.wait_for_function("document.querySelector('#rvState').textContent.includes('In review')")
    page.fill(".panel textarea", "Letter received and matches.")
    page.click('.panel .btns button:has-text("Approve case")')
    page.wait_for_function("document.querySelector('#rvState').textContent.includes('Approved')")
    page.reload()                                                           # decisions survive a reload
    page.wait_for_selector(".case")
    assert "Approved by reviewer" in page.inner_text("#rvState")


def test_notes_are_text_not_markup(page, base):
    open_sample(page, base, INTAKE)
    page.fill("#who", "<b>x</b>")
    row = page.locator(".fi.sel")
    row.locator("details.act summary").click()
    row.locator("textarea").fill('<img src=x onerror="window.__x=1">')
    row.locator(".btns button").first.click()
    page.wait_for_selector(".fi.sel .ai p")
    assert page.evaluate("window.__x") is None
    assert "<img" in row.locator(".ai p").first.inner_text()


def test_cases_list_is_worst_first_and_hides_the_name_box(page, base):
    open_sample(page, base, "Clean application")
    open_sample(page, base, INTAKE)
    page.click('nav a:has-text("Cases")')
    page.wait_for_selector("table.cases")
    risks = [int(x) for x in page.locator("table.cases tbody tr .risk b").all_inner_texts()]
    assert len(risks) >= 2 and risks == sorted(risks, reverse=True)
    assert not page.is_visible("#who")
    page.click("table.cases tbody tr:first-child a")
    page.wait_for_selector(".case .bx")
    assert page.is_visible("#who")


def test_phone_layout_has_no_sideways_scroll(page, base):
    open_sample(page, base, INTAKE)
    page.set_viewport_size({"width": 390, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.click('nav a:has-text("Cases")'); page.wait_for_selector("table.cases")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_missing_original_pdf_is_explained(page, base):
    page.route("**/documents/*/pages/*", lambda r: r.fulfill(status=404, body="{}"))
    page.goto(base + "/#/")
    page.wait_for_selector("#samples button", state="visible")
    page.click(f'#samples button:text-is("{INTAKE}")')
    page.wait_for_selector("#sheet .gone")
    assert "no longer held" in page.inner_text("#sheet .gone")


def test_dossier_strip_has_one_tile_per_document_and_opens_it(page, base):
    open_sample(page, base, INTAKE)
    assert page.locator(".dossier .tile[data-doc]").count() == 10
    assert page.locator(".dossier .tile.t-red").count() >= 1
    page.locator(".dossier .tile:not(.cur)").first.click()
    page.wait_for_selector(".tile.cur")
    assert page.locator(".bx.sel").count() <= 1
    assert not page.errors


def test_missing_document_gets_a_ghost_tile(page, base):
    open_sample(page, base, "Land use certificate not submitted")
    assert page.locator(".dossier .tile.ghost").count() == 1
    assert "Not submitted" in page.inner_text(".dossier .tile.ghost")


def test_filter_chips_change_what_is_listed(page, base):
    open_sample(page, base, INTAKE)
    issues = page.locator(".fi:visible").count()
    page.click("#fl-all")
    assert page.locator(".fi:visible").count() > issues
    page.click("#fl-attn")
    assert page.locator(".fi:visible").count() >= 1
    assert page.locator(".grp .gh .pill.s-grn").count() == 0
    assert page.locator(".grp .gh .pill.s-red").count() >= 1
    page.click("#fl-ok")
    assert page.locator(".fi:visible").count() > 1


def test_j_and_k_walk_through_issues_but_not_while_typing(page, base):
    open_sample(page, base, INTAKE)
    first = page.locator(".fi.sel").get_attribute("id")
    page.keyboard.press("j")
    second = page.locator(".fi.sel").get_attribute("id")
    assert second != first
    page.keyboard.press("k")
    assert page.locator(".fi.sel").get_attribute("id") == first
    page.fill("#who", "x")
    page.keyboard.press("j")
    assert page.locator(".fi.sel").get_attribute("id") == first


def test_decision_ladder_marks_the_deciding_step(page, base):
    open_sample(page, base, INTAKE)
    grp = page.locator(".grp:has(.fi.sel)")
    grp.locator("details.lad summary").click()
    assert grp.locator("ol.lad li.dec").count() == 1
    assert grp.locator("ol.lad li").count() == 7


def test_run_all_fills_the_case_list(page, base):
    page.goto(base + "/#/")
    page.wait_for_selector("#samples button", state="visible")
    page.click("#runAll")
    page.wait_for_selector("table.cases")
    assert page.locator("table.cases tbody tr").count() >= 7
