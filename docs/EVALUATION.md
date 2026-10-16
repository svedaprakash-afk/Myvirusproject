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
