# Docroc (prototype)

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
box on the page, to jump between them; the page image uses the same coordinates as the claim boxes. Your name on every action comes from your signed-in account (in local mode, with no sign-in, you type it at the top). Then comment, ask the institution to clarify, mark an item "checked by hand", or approve the case.
The strip at the top shows one thumbnail per document with its worst status; click one to open it. Fields in a document that share a status and reason are grouped, and **How was this decided?** shows which step of the status order applied. Filter chips narrow the list, **J** / **K** step through issues, **+** / **-** zoom the page, and **Download JSON** saves the full report. **Run all samples** on the first screen fills the case list in one click.
Every action is saved to the append-only `review_actions` table and never changes an automatic status.
Set `SAAKSHYA_DB=<file>` to keep cases, their original PDFs and reviewer actions across restarts (the newest 50 cases are kept). Without it everything lives in memory.

## What is mock
Issuers, the regulator record and the rules are MOCK or illustrative. In local mode (no sign-in configured) the reviewer name is typed, not verified: run locally only.
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

## Connect DigiLocker

Adds an **Import from DigiLocker** panel to the first screen. The user signs in at DigiLocker (OAuth 2.0 authorization code with PKCE), ticks the issued PDFs to check, and they go through the same pipeline as an upload. The access token stays on the server; the browser only holds an HttpOnly session cookie, and only files from that session's own listing can be fetched.

1. Register as a Requester at https://partners.digitallocker.gov.in (or API Setu) and register this redirect URL exactly: `https://<your-site>/digilocker/callback`.
2. Set `DIGILOCKER_CLIENT_ID`, `DIGILOCKER_CLIENT_SECRET`, `DIGILOCKER_REDIRECT_URI` (on Render: service **Environment** tab). The panel appears once all three are set. `DIGILOCKER_BASE_URL` is optional.
3. **Try it without credentials:** set `DIGILOCKER_MOCK=1`. A fake DigiLocker offers the ten synthetic "clean" documents. Never set this on a real deployment.

Limits: only PDF files are imported (XML is skipped). The check still decides each file's type from its content, so documents the rules do not know (a PAN card, a marksheet) are reported as unrecognised. Endpoint paths are at the top of `app/digilocker.py`; confirm them against the specification version DigiLocker gives you. The file integrity HMAC DigiLocker returns is not checked yet.

## Sign-in and roles

| Role | Can do |
|---|---|
| **Applicant** | Upload an application (or import from DigiLocker) and see **only their own** cases, read-only, with the reviewer's questions and decision. Cannot see other people's cases, run the samples, or record decisions. |
| **Reviewer** | See every case and its page images, comment, ask the institution to clarify, mark "checked by hand", approve, and run the samples. The name saved on each action is the account's, never typed. |
| **Admin** | Everything a reviewer can do, plus the **Users** page: create accounts, change roles, disable or enable, reset passwords. |

**How accounts are created**

1. The first admin is the `SAAKSHYA_USER` / `SAAKSHYA_PASSWORD` pair. It is re-applied at every start (created if missing, re-enabled, made admin, password reset to the variable). If an admin password is ever lost, change the variable and redeploy.
2. That admin signs in, opens **Users**, and creates reviewers, applicants and more admins (username, name, role, first password; people can change it under their name at the top right).
3. Optional: set `SAAKSHYA_ALLOW_SIGNUP=1` and the sign-in page offers **Create an account**. Sign-up can only make an **applicant**.
4. With neither variable set the app is in **local mode**: no sign-in, everyone acts as an admin, and there is no Users page.

Details: passwords are stored as salted scrypt hashes (8 to 128 characters). Sign-in sets an HttpOnly, SameSite=Lax cookie (Secure over https) for a session of up to 12 hours, held in this process's memory, so a restart signs everyone out. Five wrong passwords for one username, or 30 from one address, lock sign-in for ten minutes. Disabling an account, resetting its password or changing your own password signs the affected sessions out at once; a role change applies to a live session immediately. The last active admin cannot be disabled or demoted, and nobody can do either to their own account. Cross-site POSTs (an `Origin` that is not this host) are refused. A case an applicant may not see answers "no such case", the same as one that does not exist.

Accounts live in the same database as the cases. **On the free plan (no persistent disk) they disappear on restart**, leaving only the first admin to be recreated from the environment; use a paid plan with the `disk:` block (see below) to keep them.

## Deploy (Render, from GitHub)
1. Push this folder to GitHub (the repo root must contain `Dockerfile` and `render.yaml`).
2. On render.com: **New > Blueprint**, pick the repo, then enter `SAAKSHYA_USER` and `SAAKSHYA_PASSWORD` when asked. They create the first admin (see *Sign-in and roles*). Optionally set `SAAKSHYA_ALLOW_SIGNUP=1` to let applicants register themselves.
3. Open the URL Render gives you, sign in as that admin, and create reviewer accounts on the **Users** page.

* Without those two variables the app has no login, so only run it that way locally.
* Keep one worker: the work queue and caches live in this process. The Docker image saves cases to `/data/saakshya.db`, but the free plan has no persistent disk, so a restart still clears them. To keep them, use a paid plan, uncomment the `disk:` block in `render.yaml` (mounted at `/data`) and redeploy.
* Checks run off the web thread, one upload at a time. Up to 8 may wait; more get a "server is busy, try again" message instead of freezing the site. PDFs over 60 pages are refused, and OCR gives up on a page after 60 seconds.
* `/healthz` is always open (no sign-in) for the platform's health check.
