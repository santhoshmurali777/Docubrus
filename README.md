# Docubrus (prototype)

Checks the documents of an institution's application: reads each PDF (text or scan), verifies its proof of
authenticity, compares facts across documents, and gives every extracted value one status with a reason and a
location on the page.

## Install
    pip install -r requirements.txt
    # OCR needs the tesseract program:  Ubuntu/Debian: sudo apt install tesseract-ocr
    #                                   macOS: brew install tesseract   Windows: UB-Mannheim installer, add to PATH

## Run
    python -m app.documents.make_synthetic     # (re)generate the seeded demo data
    uvicorn app.main:app                       # open http://127.0.0.1:8000
    pytest                                     # scan tests take about a minute
    # browser tests (tests/test_review_ui.py) need:  pip install playwright && playwright install chromium   (skipped if absent)

## Reviewer screen
Open a case from the result or from **Cases** (highest risk first). Click **Show on page** on any finding, or click a
box on the page, to jump between them; the page image uses the same coordinates as the claim boxes. Enter your name
at the top, then comment, ask the institution to clarify, mark an item "checked by hand", or approve the case.
The strip at the top shows one thumbnail per document with its worst status; click one to open it. Fields in a document that share a status and reason are grouped, and **How was this decided?** shows which step of the status order applied. Filter chips narrow the list, **J** / **K** step through issues, **+** / **-** zoom the page, and **Download JSON** saves the full report. **Run all samples** on the first screen fills the case list in one click.
Every action is saved to the append-only `review_actions` table and never changes an automatic status.
Set `SAAKSHYA_DB=reviews.db` to keep actions across restarts (the cases themselves are still in memory only).

## What is mock
Issuers, the regulator record and the rules are MOCK or illustrative. No login yet, so the reviewer name is typed, not verified: run locally only.
See `data/synthetic/EXPECTED.md` for seeded scenarios and known limits.

## W2: real verification paths

* **Embedded digital signatures (PAdES).** `app/authenticity/pades_verifier.py` checks present / valid / bound / fresh with pyHanko, offline.
  To use it for a document type: put the issuer's root certificate (PEM) in `config/trust_roots/`, register it in `config/pades_roots.json`,
  then set that type's `proof.kind` to `"pdf_signature"` and `required_sources` to `[]` in `document_registry.json`.
  No default document type uses it yet. Check which of your real documents ship signed before switching one.
  **Not checked:** revocation (CRL/OCSP) and trusted timestamps. Every Authenticated result says so in its flags.
* **Which of my documents are signed?** `python -m app.authenticity.signature_survey <folder-or-pdfs>` lists, per PDF, whether it carries an
  embedded signature, who signed and who issued it, the signing time, and whether it is intact. It trusts nothing: it only tells you which
  authority's root certificate to ask for. Run it on your real certificates before switching any type to `pdf_signature`.
* **Test fixtures.** `python -m app.authenticity.pades_fixture` signs a synthetic PDF with a throw-away root (`data/pades/`). These are fixtures, not real documents.
* **Adapters.** `app/authenticity/adapters.py` defines `IssuerAdapter` (`verify`, `lookup`) with `MockIssuerAdapter`, `PadesAdapter`, and a `LiveIssuerAdapter` placeholder. The orchestrator picks one from `proof.kind`.
* **Regulator snapshot.** `python -m app.aicte_snapshot import <csv> <out.json> --date YYYY-MM-DD --origin "<source>" --key-col .. --name-col .. --intake-col .. --intake-mode sum|first|max`,
  then set `SAAKSHYA_AICTE_SNAPSHOT=<out.json>`. Reports then say "snapshot dated X", and a note appears if it is over 90 days old.

## Deploy (Render, from GitHub)
1. Push this folder to GitHub (the repo root must contain `Dockerfile` and `render.yaml`).
2. On render.com: **New > Blueprint**, pick the repo, then enter `SAAKSHYA_USER` and `SAAKSHYA_PASSWORD` when asked.
3. Open the URL Render gives you and sign in with that user and password.

* Without those two variables the app has no login, so only run it that way locally.
* Keep one worker: cases and PDFs live in memory, and a restart clears them. The free plan also sleeps when idle and has no persistent disk, so reviewer actions are lost on restart. To keep them, use a paid plan with a disk mounted at `/data` and set `SAAKSHYA_DB=/data/reviews.db`.
* `/healthz` is always open (no password) for the platform's health check.
