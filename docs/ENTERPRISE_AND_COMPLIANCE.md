# Compliance, controls and integrations added after the first release

Everything here is covered by automated tests. **What the tests do not prove is stated per item.** Items marked ⚠ were written from a published
format or specification and have never been run against the real system named: do one supervised first run before relying on them.

## Matching quality
| Feature | What it does | Not proven |
|---|---|---|
| Per-customer trust | A customer starts in a learning phase (every match needs a person) and graduates to straight-through after N consecutive confirmed matches with no correction. A rejection, manual override or reversal demotes; a reversed auto-match also clears a manager's pin. Managers can pin a customer to straight-through or suggest-only and raise (never lower) the confidence needed. Off by default. | The right N for your data. Start at 5 and watch Model health. |
| Model health | Population-stability of confidence, reviewer overturn rate, reversed auto-matches, calibration error, with an explicit "not enough data" state. | Thresholds are conventional (PSI 0.1 / 0.25), not tuned. |
| Shadow test | A candidate model is scored next to the active one on live proposals and compared once people have labelled them. It never decides anything and is never promoted automatically. | Labels exist only for reviewed items and only for candidates the active model produced. |
| On account & advances | Excess cash and advances become balances; later invoices are suggested against them (a PO/reference the payer quoted wins, else oldest due); a person applies each one; it posts Dr unapplied cash / Cr receivables. Foreign-currency balances and disputed invoices are never suggested. **Also fixes a defect**: holding an overpayment on account used to post the whole bank receipt a second time. | |
| Remittance scans | Local OCR (Tesseract) for images and image-only PDFs; header-aware table reading for XLSX/CSV/PDF/OCR text; self-check that lines add up to the stated total; a poor scan is capped at low confidence and flagged. An optional LLM is consulted only when the local result is not confident, and only if its answer validates and beats the local one. | OCR quality on your customers' real advices. |

## Tax
| Feature | What it does | Not proven |
|---|---|---|
| 194Q | Deduction only on the slice of a financial year's purchases above the threshold (default ₹50 lakh, configurable). | The threshold and 0.1% are statutory values: confirm with your tax team. |
| GST-TDS | A combined deduction (e.g. 194C + GST-TDS) is recorded as two entries and posted to two ledgers; only income-tax TDS needs a Form 16A. | |
| TDS register | By year, quarter, customer and section with Form 16A due dates (return due date + 15 days) and overdue flags; CSV export. | Extensions notified by the CBDT are not tracked. |
| Form 26AS / AIS ⚠ | Imports the TRACES text file or a CSV/XLSX export, maps deductors to customers by TAN or an unambiguous name, and reconciles by quarter: matches, quarter-timing, we claimed more, not reported, statement higher. Provisional lines are shown separately. | Never run on a real downloaded file. AIS JSON and PDF are not read. TCS (Part B, 206C) is deliberately ignored. |
| GST filing checks ⚠ | Compares open invoices with imported GSTR-1 JSON and an e-invoice register export: not reported, GSTIN or value differs, IRN cancelled, IRN missing (when e-invoicing applies). | Never run on real downloads. GSTR-2B is the customer's view of your invoices and cannot be downloaded by a supplier, so it is not parsed. |
| Ageing and provision | Buckets by days past due, credits and cash on account netted against the oldest items, provision matrix, higher minimum on disputed invoices, foreign currency at the as-of rate, CSV export. | The default matrix is illustrative: set yours from loss history and your auditor's view. |

Not built: TCS under section 206C(1H) (omitted by the Finance Act 2025 as far as we know: verify), GST returns filing, e-invoice generation.

## Controls
- **Period close**: books closed through a date; later-posted items dated inside it are posted on the first open day (or refused: `period_lock_policy`). Re-opening needs a reason, is audited and alerts.
- **Auditor evidence pack** (Audit trail > Evidence pack): a zip with approvals, match decisions with model version and guardrails, posting batches and lines, segregation-of-duties findings, configuration, users and roles (no credentials) and the audit trail, with a SHA-256 manifest and the hash-chain check done at export time.
- **Alerts**: email (SMTP), Slack and Teams through a durable outbox with retry and back-off. Events: approval waiting, exception past due, model alert, batch failed, ERP has not confirmed, auto-match reversed, period re-opened. Secrets are read from environment variables you name. Teams uses the plain `{"text": …}` payload and is unverified against a current tenant.

## Access
- **OpenID Connect sign-in** (authorization code + PKCE, id_token checked against the provider's keys, RS256/ES256 only, issuer/audience/expiry/nonce, role from a group map, optional enforcement with an admin break-glass). Tested against a local mock provider, **not a live tenant**. SAML is not supported.
- **SCIM 2.0** users: create, update, deactivate; Entra-style PATCH; deprovisioning takes effect at the next request. SCIM can never grant finance-head or admin (configurable via `SCIM_ALLOWED_ROLES`).

## ERP and bank connectivity ⚠
| Connector | Output | Notes |
|---|---|---|
| Tally Prime | XML Receipt/Journal vouchers with bill allocations (Agst Ref / On Account) | Import into a test company first. |
| SAP | BAPI_ACC_DOCUMENT_POST-shaped JSON for your middleware | Does **not** clear open items in SAP. |
| Business Central | general journal lines JSON | Field names follow the v2.0 API pages. |
| NetSuite | journal-entry CSV for the Import Assistant | |
| ERP confirmation | CSV from the ERP acknowledges a batch only when every voucher is confirmed; rejected vouchers can fail the batch; unconfirmed exports alert | |
| SFTP host-to-host | polls a remote directory; **the server's host key must match a pinned SHA-256 fingerprint before any credential is sent**; archive folder optional | Tested against a real in-process SFTP server. |
| S3 Object Lock | write-once retention on posting exports (GOVERNANCE or COMPLIANCE) | Tested with moto, not against AWS, Ceph or MinIO. Bucket must be created with Object Lock. |

Not built: Account Aggregator (requires a licensed financial-information user registration), Zoho Books, bank-specific APIs.
MT940 and camt.053 statement parsers were already part of the first release.
