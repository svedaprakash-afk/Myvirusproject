# Evaluation: what the numbers do and do not mean

`python -m app.seed.evaluate --seed N` runs ~190 synthetic bank lines (NEFT/RTGS/IMPS/UPI/cheque/gateway, TDS short-pays,
bundles, credit-note netting, duplicates, typo'd references, confusable customer names, third-party payers) through the real
ingestion and matching pipeline and compares against ground truth.

Typical result (seeds 1-6): **0 unsafe auto-approvals, 100% auto-approval precision, 53-69% of matchable receipts straight-through**,
the rest routed to review with the right invoices proposed.

**Be sceptical of these numbers.** The generator and the matcher were written by the same authors; the generator encodes the
patterns the matcher knows. They prove internal consistency and guard against regressions (the test-suite fails if
`unsafe_auto != 0`). They are **not** evidence of accuracy on real bank data. Adversarial scenarios were added after the first run
looked too good, and they found real defects (spurious TDS "explanations", conflicting-reference ranking, third-party payers);
real data will find more.

## Before trusting it with real money
1. Run in **suggest-only mode** (Settings -> disable straight-through) on 4-8 weeks of a design partner's *historical* statements and open items.
2. Measure precision of what *would* have auto-posted, per customer. Enable auto-approval only when it is ~100% and review volume is acceptable.
3. Enable amount-only auto-approval separately, last, if at all.
4. Re-measure after every model promotion. Models are versioned and instantly roll-backable.

## Coverage added in the second iteration
- `tests/narration_corpus.py`: 23 narration layouts modelled on formats used by major Indian banks (NEFT/RTGS/IMPS/UPI/cheque/ACH/SWIFT, SBI/ICICI/HDFC/Axis/Kotak/Yes styles). These are written from knowledge of the formats, **not** taken from real statements; a real bank's file may differ, which is exactly what the profile preview and custom narration rules are for.
- Multi-currency, mailbox, feed, mapped-export, webhook and residency behaviour is covered by unit/integration tests using fakes (fake IMAP server, fake HTTP transports, temp directories). None has been run against a real bank, ERP, mail server or cloud account.
- The synthetic-ground-truth harness (`python -m app.seed.evaluate`) covers INR matching only; the foreign-currency scenarios in the demo data are exercised by tests, not scored.

## Third iteration
- Bank layouts (HDFC, ICICI, SBI, Axis, Kotak, Yes, IDFC FIRST, PNB; BoB/Canara/IndusInd templates only) and 40 narration styles were written **from memory** and are marked unverified in the UI. `tests/test_bank_layouts.py` passing means the parser matches those recollections.
- The FBIL parser is tested on fixtures written from secondary descriptions; the live format is unverified.
- Scheduling (FX window, retries, slot locks, inbox/bank sync) is tested with injected clocks and fake transports.

## Fourth iteration
- The whole suite and the migrations were run against a **local PostgreSQL 16**, not just SQLite. That found a real bug SQLite hides (batch-cancel overflowed a `VARCHAR(64)`), now fixed, and drove input-length hardening. It is still a scratch server: not a managed service, not your data, not your network.
- Top-10 currencies: six of the ten INR rates are derived (FBIL EUR/INR ÷ ECB EUR/currency). Fixtures for the ECB XML were written from memory of its public format; the live feed is unverified.
- A yen test exposed and fixed two defects: foreign-currency shortfalls were being "explained" as Indian TDS, and explanations used ₹ for non-INR amounts.

## Fifth iteration
- Object storage is tested with **moto**, in-process (unit/integration tests) and as a real HTTP S3 server (browser journey: export → stored with SSE and SHA-256 → verified download → storage self-test). That proves the S3 API usage, not behaviour on real AWS/Oracle/Ceph/MinIO, nor IAM/bucket-policy/KMS/network setup. Use **Test storage** on your real bucket.
- AED is derived from FBIL USD/INR and the configured 3.6725 peg (from memory; verify, and note a peg can change). Neither FBIL nor the ECB publishes AED.
- Writing the adoption test for the new migration exposed a flaw in legacy-database handling (it always stamped the baseline); it now stamps the newest revision the database already satisfies.
- Full suite: 291 tests pass on PostgreSQL 16 and on SQLite (4 are Postgres-only).
