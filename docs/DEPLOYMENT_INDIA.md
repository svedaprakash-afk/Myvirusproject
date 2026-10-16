# Deploying with Indian data residency

Provider-neutral deployment steps, manifests and the AWS/Azure/GCP/Oracle/on-prem mapping are in [`deploy/README.md`](../deploy/README.md). This page covers the residency controls and obligations.

**Read this first.** The application enforces what *it* can: it will not start outside an Indian region in strict mode and it
refuses outbound connections to hosts you have not allow-listed. It **cannot** verify where your database, backups, logs, object
storage or e-mail provider physically are. That is a property of how you deploy it, and the checklist below is yours to complete.
This document is engineering guidance, not legal advice: have counsel confirm obligations under the DPDP Act 2023 and any RBI
directions that apply to *your* entity type (the RBI's 2018 payment-data storage directive is aimed at payment-system operators and
their service providers; whether it reaches you depends on how you use this system).

## What the software enforces (`DATA_RESIDENCY_STRICT=1`)
| Control | Behaviour |
|---|---|
| Startup | Aborts unless `DEPLOY_REGION` ∈ {ap-south-1, ap-south-2, centralindia, southindia, westindia, asia-south1, asia-south2, ap-mumbai-1, ap-hyderabad-1, in-west, in-central, on-prem-india} |
| Egress | Bank REST feeds, ERP webhook, IMAP and the optional LLM extractor can reach **only** hosts in `ALLOWED_EGRESS_HOSTS` (exact host, or `.suffix`) |
| LLM extraction | Disabled in strict mode unless `api.anthropic.com` is explicitly allow-listed. Remittance text then leaves India — decide deliberately. Matching never needs it |
| Configuration | Saving an ERP webhook, mailbox or bank feed pointing at a non-allow-listed host is rejected (422) |
| `/health` | Reports region and strict flag, so a probe can alarm on drift |

`DEPLOY_REGION` is a *declaration* by the operator. Nothing stops a mis-declared deployment, so enforce the region in infrastructure too.

## Infrastructure checklist (you must do these)
- [ ] **Compute** in an Indian region (AWS Mumbai `ap-south-1` / Hyderabad `ap-south-2`; Azure Central/South India; GCP Mumbai/Delhi `asia-south1/2`).
- [ ] **Database**: managed Postgres in-region, encryption at rest with a customer-managed key (KMS) created **in-region**; TLS only.
- [ ] **Backups & DR**: snapshots, WAL archives and the DR replica in an Indian region (e.g. Mumbai ↔ Hyderabad). **No cross-border replication**; check the provider's default "global" backup/replication options are off.
- [ ] **Object storage** for `EXPORT_DIR` / feed landing zones: same-region bucket, private, versioned, SSE-KMS.
- [ ] **Logs / metrics / traces / error tracking**: in-region sink. Application logs avoid payload content, but confirm your APM does not capture request bodies; many SaaS APMs are hosted abroad.
- [ ] **E-mail**: the remittance mailbox must be hosted in India or its provider contractually committed to Indian storage. A mailbox on a foreign SaaS means remittance data already left India before we poll it.
- [ ] **Network egress**: default-deny egress at the VPC/firewall as well as `ALLOWED_EGRESS_HOSTS` (defence in depth). Allow only: your ERP, bank endpoints, the mailbox host, the container registry/OS mirrors.
- [ ] **Secrets**: in-region secrets manager; reference by environment variable name (`password_env`, `token_env`, `secret_env`) — the app never stores credentials.
- [ ] **Admin access**: bastion / SSO in-region; production data never copied to laptops or foreign CI.
- [ ] **Sub-processors list** maintained; each one's storage region documented.
- [ ] **Container images / dependencies** pulled from an in-region mirror if your policy requires.

## Personal-data handling (DPDP Act 2023) — items to settle with counsel
This system holds business-contact data (payer names, e-mail senders, UPI handles, bank account numbers of counterparties). Items to decide: lawful basis/notice for counterparties; purpose limitation; **retention** (nothing is deleted today — no retention job exists); **erasure/correction** workflow (none built; audit log is deliberately immutable, so design which fields are erasable); breach-notification runbook; data-fiduciary vs processor roles in your contracts; cross-border transfer rules if strict mode is ever relaxed.
Technical measures present: TLS expected in front of the API, bank-account masking by role, RBAC, hash-chained audit log, PII redaction before any LLM call. Not present: field-level encryption at rest, retention/erasure automation, SSO/MFA, rate limiting.

## Example production environment
```
DATABASE_URL=postgresql+psycopg://app:***@mydb.xxxx.ap-south-1.rds.amazonaws.com/cashapp?sslmode=require
JWT_SECRET=<64 random bytes>      APP_ENV=prod      ALLOW_DEMO=0
DATA_RESIDENCY_STRICT=1           DEPLOY_REGION=ap-south-1
ALLOWED_EGRESS_HOSTS=erp.corp.example.in,imap.corp.example.in,api.yourbank.co.in
FEED_ROOT=/data/feeds             EXPORT_DIR=/data/exports
```
The full test-suite and the migrations have been run against PostgreSQL 16 (see CI `backend-postgres`), but never against a managed service or your data; run a rehearsal restore and upgrade on a copy before go-live.

## FX reference-rate feed and residency
The feed calls the host in `FX_FEED_URL` (FBIL's publication). If you also set `FX_CROSS_FEED_URL` (the ECB's daily euro rates, used to derive CNY/AUD/CAD/CHF/HKD/SGD), that host is outside India too and must be allow-listed on the same basis (public data in, nothing out). In strict mode that host must be in `ALLOWED_EGRESS_HOSTS` — adding `www.fbil.org.in` is a deliberate, narrow exception: the call sends no customer data, it only downloads public rates.
