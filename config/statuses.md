# Status definitions and check order

**Purpose:** the single source of truth for what each status means and the order
`app/status.py` must check them in. The team signs this off *before* `status.py`
is written. If code and this file disagree, this file wins.

**Scope:** one status per *claim*. A claim is one fact extracted from one document
(for example `approved_intake = 120` on page 2 of the sanction letter).

**Inputs the evaluator receives:** the claim, its evidence list, the source
responses, the authentication result of the claim's document, the applicable rule,
and `today` (the date of the check, used as the application date).

---

## The seven statuses

| Status | Plain meaning | Reviewer should |
|---|---|---|
| **Missing** | A required document or field was expected and nothing was found. | Ask the institution to supply it. |
| **Blurry scan** | The claim was read from a scan too unclear to rely on, so we cannot say what it really says. | Ask for a clearer re-upload. |
| **Expired** | The claim has a validity date earlier than `today`. | Ask for a current certificate. |
| **Contradicted** | The claim conflicts with another document or record, or its document's proof is invalid. | Investigate the conflict. |
| **Source unavailable** | A source the checks depend on could not be reached, so the claim cannot be judged right now. | Retry later. Do not treat as pass or fail. |
| **Insufficient evidence** | The claim exists but is not backed strongly enough to rely on. | Request better proof or review by hand. |
| **Verified** | Every condition in the list below holds, with recorded evidence. | Spot-check if desired. |

## What "Verified" requires (ALL must be true)

1. The claim is grounded: it has a document, a page and a location.
2. The claim passed the scan-quality gate (it was read reliably).
3. The claim is not expired on `today`.
4. The claim's document authentication result is **Authenticated**.
5. At least one **strong** evidence item that **qualifies** is recorded for the claim.
6. No contradiction was found against any other document or record.
7. No source that the document type requires failed or was unreachable.

Anything less is not Verified. There is no partial credit and no "probably".

---

## Check order (first match wins)

`status.py` evaluates in exactly this order and stops at the first match.

| Step | Condition | Result |
|---|---|---|
| 1 | A required claim or document is absent | **Missing** |
| 2 | The claim came from a page that failed the scan-quality gate (unreliable reading) | **Blurry scan** |
| 3 | The claim has a validity date earlier than `today` | **Expired** |
| 4 | A conflict with another document or record exists, **or** the document's authentication result is *Proof invalid* | **Contradicted** |
| 5 | A required source failed, **or** the document's authentication result is *Issuer unreachable* | **Source unavailable** |
| 6 | No strong qualifying evidence, **or** the document's authentication result is *Proof absent* or *Not software-verifiable* | **Insufficient evidence** |
| 7 | None of the above | **Verified** |

### Why this order

- **Missing first:** there is nothing to judge.
- **Blurry scan before Expired and Contradicted:** if a scan is too blurry to
  read the date or number, we cannot claim it is expired or conflicting. We say
  we cannot read it, with its own status so the institution knows to re-upload
  a clearer copy and not to look for a missing proof.
- **Expired before Contradicted:** an out-of-date certificate is a hard fact the
  institution must fix, whatever else is true.
- **Contradicted before Source unavailable:** a conflict between documents we
  already hold is real even when some other source is down.
- **Source unavailable before Insufficient:** "we could not check" must never be
  confused with "we checked and it was weak".
- **Verified last:** it is only reachable when every earlier step has said no.
  This is the guarantee that no unsupported claim can appear verified.

---

## How document authentication results map to statuses

| Authentication result | Effect on its claims |
|---|---|
| Authenticated | No effect by itself. The claim continues through the order. |
| Proof invalid | **Contradicted** (step 4). The document's own proof does not hold. |
| Issuer unreachable | **Source unavailable** (step 5). |
| Proof absent | **Insufficient evidence** (step 6). |
| Not software-verifiable | **Insufficient evidence** (step 6). Goes to a human. |

## Evidence strength rules

- **Strong evidence** (valid signature, confirmed issuer record, registry match
  bound to the document's data) can support Verified.
- **Weak evidence** (number format, plausibility, template match, forensic traces,
  semantic review) can **never** support Verified. It may be shown to the reviewer
  and can explain an Insufficient evidence result.
- If strong qualifying evidence exists, weak evidence does not override it. The
  reviewer still sees the full evidence stack.

## Every finding must explain itself

The `reason` on each finding states which step of the order produced the status
and which evidence or conflict caused it. If several steps would apply, the
first one decides the status and the others are listed in the reason.

---

## Reviewer actions never change a status

A human reviewer can record a comment, a clarification request, an "I checked this by hand"
attestation, and a case-level approval. These are stored in their own append-only table
(`review_actions`) with the reviewer's name and the time, and they are shown *next to* the
findings. They do **not** create, change or override any finding, and none of them can make a
claim Verified. Only evidence recorded through the ledger can do that.

- A case cannot be approved while a clarification is open.
- Approving a case that still has Contradicted, Expired or Missing items needs a written reason.
- "Approved by reviewer" is a case decision, not a status. It is never shown as "Verified".
- (Open point for W3) a `reviewer_attested` strong-evidence kind that *can* qualify a claim is
  **not** built. When it is, this file must be updated first.

---

## Seeded examples (from the synthetic data pack)

| Case | Expected status | Decided at step |
|---|---|---|
| Intake 120 in the sanction letter, 60 in the mock AICTE record | Contradicted | 4 |
| Fire NOC with a validity date before `today` | Expired | 3 |
| Blurry scanned certificate | Blurry scan | 2 |
| Required document not uploaded | Missing | 1 |
| Valid QR copied onto a different document (binding fails) | Contradicted | 4 |
| Mock AICTE record switched to outage | Source unavailable | 5 |
| Land deed with no proof of any kind | Insufficient evidence | 6 |
| Fully authenticated, consistent, current document | Verified | 7 |

## Open points for sign-off

1. **Scan-quality threshold:** starting value is an average OCR confidence of 60.
   Tune it on the synthetic scans.
2. **`today`:** the date the check runs. Decide later whether the real system uses
   the application submission date instead.
3. **Weak evidence overriding strong evidence:** currently it does not (see above).
   Confirm that is the intended behaviour.
4. **Required sources:** which sources each document type requires is defined in
   `config/document_registry.json`, not here.
