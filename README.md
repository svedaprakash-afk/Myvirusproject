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

## Run it
```bash
python -m venv .venv && . .venv/bin/activate && pip install -r backend/requirements-dev.txt
cd backend && python -m app.cli seed-demo          # synthetic tenant (sqlite ./cashapp.db)
uvicorn app.main:app --port 8000 &                 # API docs at /docs
cd ../frontend && npm install && npm run dev       # http://localhost:5173
```
Demo logins (password `demo1234`): analyst@ / manager@ / treasury@ / cfo@ / auditor@ / admin@`meridian.example`.

Tests: `cd backend && pytest` (160) and `cd frontend && npm test && npm run build`.
Production: `docker compose up` with `JWT_SECRET` set; create the first user with `python -m app.cli create-user`.

## Architecture
`ingestion/` parsers + idempotent persistence -> `matching/` (context, identify, references, subset, tds, scoring, engine, actions) ->
`workflow.py` (approvals, exceptions, manual match) -> `posting/` (canonical journal, connectors) -> `analytics.py`; REST in `api/`;
React/TypeScript UI in `frontend/`. Money is integer paise end to end.

## Honest limits
- **Accuracy is unproven on real data** — see `docs/EVALUATION.md`. Start in suggest-only mode.
- **No bank-specific or ERP-specific connector has been tested against a real system.** The generic mechanisms (folder drop, REST/JSON,
  mapped CSV, signed webhook, IMAP) are tested with fakes/temp files only. Native SFTP polling and bank-specific APIs are *planned* and refuse
  to run; Tally/Zoho/Business Central/SAP have no dedicated adapters (use mapped CSV or the webhook). e-invoice portal and GST/TRACES/26AS
  integrations are not built.
- **Multi-currency scope**: 2-decimal currencies only (no JPY-style 0-decimal handling); rates are entered by you (no feed); realised FX is
  computed from booking vs settlement rates and cross-currency matches never auto-approve unless you opt in *and* the payer stated the
  foreign amount. Manual cross-currency allocation is refused. No unrealised-FX revaluation.
- **Data residency is only partly enforceable in software** — see `docs/DEPLOYMENT_INDIA.md`.
- Statutory TDS rates are **defaults for matching hints**, configurable per customer; confirm with your tax team.
- Not built: schema migrations (Alembic), SSO/MFA, field-level encryption at rest, background job queue, multi-currency FX revaluation,
  customer portal, email-inbox connector, reversal of already-posted receipts, rate limiting. Postgres config is provided but untested.
- The optional LLM remittance extractor (set `ANTHROPIC_API_KEY`) redacts PII and validates output against the source text, but has
  only been tested with a fake transport, not the live API. Matching never depends on it.
- Seed data and the evaluation harness are synthetic.
