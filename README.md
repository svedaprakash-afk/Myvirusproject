# Cash Application & Receivables Exceptions (India)

AI-assisted cash application for Indian mid-market corporates: turns messy bank statements, remittance advices, GST/e-invoice and
ERP open items into matched, audit-ready cash postings, with exceptions, maker-checker controls and TDS tracking.

**Status: working vertical slice, not a production SaaS.** See *Honest limits* below.

## What it does
- **Bank-agnostic**: per-account *statement profiles* (columns, delimiter, date format, signed/DR-CR amounts, custom narration regex rules)
  with a dry-run **preview** and column auto-detection, so a new bank is onboarded by configuration. Feeds: folder drop (SFTP landing
  zone) and generic REST/JSON; plus built-in CSV/XLSX, MT940, camt.053 (XXE-safe) and gateway settlements. Idempotent re-import.
- **ERP-agnostic**: one canonical balanced journal; export via a *column-mapped CSV* (any layout, delimiter, decimal/date format, ledger map)
  or a *signed webhook* (HMAC, idempotency key, auto-acknowledge); open-items import through a configurable mapping (CSV/XLSX).
- **Remittance inbox**: IMAP poller + .eml intake; idempotent on Message-ID; SPF/DKIM/DMARC verdict + domain allow-list, with
  unauthenticated mail **quarantined** until a person releases it; HTML/PDF/XLSX/CSV attachments; authenticated senders are learned.
- **Multi-currency**: foreign-currency invoices with booking rates, FX rate table, same-currency foreign receipts (intermediary bank
  charges handled), **cross-currency** receipts (e.g. USD invoice paid in INR from the bank's inward-remittance narration or the rate
  table), realised FX gain/loss computed into balanced base-currency journals, per-currency tolerances, base-currency dashboards.
- **FX reference-rate feed**: the RBI reference rate is now computed and published by **FBIL**; a tolerant parser (CSV/TXT/HTML/XLSX, JPY per 100), daily scheduled fetch after 14:00 IST with bounded retries, manual file import, a 5% day-on-day circuit breaker, stale-rate warnings. **The live URL/layout is unverified — see `docs/DEPLOYMENT_INDIA.md`.**
- **Worker**: `python -m app.cli worker` runs FX fetch, inbox polling, bank-feed sync and matching on schedule; slot-locked so replicas never double-run.
- **Cloud-agnostic India deployment**: hardened containers, Kubernetes manifests with default-deny egress, compose file, CI workflow (`deploy/`).
- **Object-storage exports**: posting exports and the bank-file landing zone live in S3-compatible storage (or a local directory). SSE/KMS, SHA-256 integrity at write and at every download, a batch is only "exported" once its file is stored and read back, verified downloads, a storage self-test, and strict-mode residency checks. No shared volume needed.
- **AED** (and other USD-pegged currencies via `FX_PEGS`) derived from FBIL USD/INR and the peg; neither FBIL nor the ECB publishes it.
- **Schema migrations (Alembic)**: reviewed baseline, deterministic constraint names, drift guard in tests/CI, safe adoption of pre-Alembic databases, a Postgres advisory lock so many pods can start at once, and a production start-up guard that refuses a stale schema (`python -m app.cli migrate | status | create-migration | downgrade`).
- **Top-10 currencies**: USD, EUR, JPY, GBP, CNY, AUD, CAD, CHF, HKD, SGD. FBIL publishes four; the other six are *derived* cross rates (FBIL EUR/INR ÷ ECB EUR/currency), cross-checked against FBIL's USD, labelled non-official.
- **Data residency**: strict mode (Indian region required, egress allow-list, LLM off by default) + `docs/DEPLOYMENT_INDIA.md`.
- **Narration parsing**: NEFT/RTGS/IMPS/UPI/cheque/ACH/SWIFT layouts used by the major banks (23-case corpus), invoice ranges.
- **Understand Indian narrations**: NEFT/RTGS/IMPS/UPI/cheque/ACH, UTR/RRN, payer names, VPAs, IFSC, virtual accounts (VAN).
- **Match**: payer identification (VAN, GSTIN, learned VPA, fuzzy name) -> invoice references (full number, IRN, PO, bare serial)
  -> bounded subset-sum for bundles -> TDS-aware shortfall explanation (taxable-value base, customer's usual rate, rupee-rounding)
  -> credit-note netting -> tolerance write-offs. Candidates are scored by a transparent, versioned logistic model.
- **Control**: hard guardrails independent of the model decide what may post without a human (ambiguity, third-party payer,
  disputed invoice, residuals, unusual TDS, amount-only, ceilings...). Maker-checker approvals by amount; segregation of duties on
  batch build/release; hash-chained audit log with verification; RBAC with PII masking.
- **Resolve**: exception queue (unidentified, short, over, duplicate, intercompany, non-AR) with SLA/priority and one-click actions.
- **Close the loop**: balanced canonical journals -> pluggable ERP connectors (generic CSV, canonical JSON) -> acknowledgement.
- **Learn**: analyst accept/reject/reverse feed a retrainable model; candidates are never self-activating; rollback in one click.
- **TDS tracker**, DSO/ageing/unapplied-cash dashboards, collections projection.
- **Added since**: per-customer straight-through trust, model health and shadow testing, on-account/advance application, scanned-remittance OCR with
  reconciliation checks, 194Q threshold and GST-TDS split, TDS register with Form 16A dates, Form 26AS/AIS reconciliation, GST filing checks,
  ageing and provision, period close, auditor evidence pack, alerts (email/Slack/Teams), OpenID Connect sign-in and SCIM, Tally/SAP/Business Central/NetSuite
  renderers, ERP confirmation files, SFTP host-to-host, S3 Object Lock. **Read `docs/ENTERPRISE_AND_COMPLIANCE.md` for what each does and what is unproven.**

## Run it
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements-dev.txt
cd backend && python -m app.cli seed-demo          # synthetic tenant (sqlite ./cashapp.db)
uvicorn app.main:app --port 8000 &                 # API docs at /docs
cd ../frontend && npm install && npm run dev       # http://localhost:5173
```
Demo logins (password `demo1234`): analyst@ / manager@ / treasury@ / cfo@ / auditor@ / admin@`meridian.example`.

Tests: `cd backend && pytest` (291; `TEST_DATABASE_URL=postgresql+psycopg://… pytest` runs the same suite on PostgreSQL) and `cd frontend && npm test && npm run build`.
Production: `kubectl apply -k deploy/k8s` (see `deploy/README.md`) or `docker compose up` with `JWT_SECRET`, `DEPLOY_REGION` set; create the first user with `python -m app.cli create-user`.

Static demo (no backend, synthetic data): seed and run the API, then `python -m app.seed.demo_scenario http://localhost:8000` (drives the tenant into a state that shows
the newer features), `python -m app.seed.export_snapshot http://localhost:8000 frontend/src/demo/snapshot.json`, and
`cd frontend && npx vite build --config vite.demo.config.ts && node scripts/inline-demo.mjs out.html`.

## Architecture
`ingestion/` parsers + idempotent persistence -> `matching/` (context, identify, references, subset, tds, scoring, engine, actions) ->
`workflow.py` (approvals, exceptions, manual match) -> `posting/` (canonical journal, connectors) -> `analytics.py`; REST in `api/`;
React/TypeScript UI in `frontend/`. Money is integer paise end to end.

## Honest limits
- **Accuracy is unproven on real data** — see `docs/EVALUATION.md`. Start in suggest-only mode.
- **No bank-specific or ERP-specific connector has been tested against a real system.** The generic mechanisms (folder drop, REST/JSON,
  mapped CSV, signed webhook, IMAP) are tested with fakes/temp files only. The Tally/SAP/Business Central/NetSuite renderers, the 26AS/AIS and
  GSTR-1/e-invoice readers were written from published formats and have never met a real system or a real download. Bank-specific APIs and
  Account Aggregator are not built; Zoho Books is planned and refuses to run. Native SFTP polling is tested against a real SFTP server.
- **Multi-currency scope**: all arithmetic uses 1/100 of the unit, so JPY works but is carried as 1/100 yen (no fractional-yen semantics) and 3-decimal currencies (KWD, BHD) are unsupported; the six non-FBIL top-10 rates are *derived* (indicative, not official); rates come from the FBIL feed/import or manual entry; realised FX is
  computed from booking vs settlement rates. Cross-currency matches auto-approve (default ON) only when the payer stated the foreign amount; estimates never do. Manual cross-currency allocation is refused. No unrealised-FX revaluation.
- **Data residency is only partly enforceable in software** — see `docs/DEPLOYMENT_INDIA.md`.
- Statutory TDS rates are **defaults for matching hints**, configurable per customer; confirm with your tax team.
- Not built: SAML, MFA, Azure Blob (not S3-compatible) storage backend, field-level encryption at rest, background job queue, multi-currency FX revaluation,
  customer portal, reversal of already-posted receipts, rate limiting. 
- The optional LLM remittance extractor (set `ANTHROPIC_API_KEY`) redacts PII and validates output against the source text, but has
  only been tested with a fake transport, not the live API. Matching never depends on it.
- Seed data and the evaluation harness are synthetic.
