# Legacy invoice / PaymentIntent reconciliation — release gate

## Why this patch exists

The production Railway receiver is running the older `main` application with a persistent SQLite volume. That implementation recorded paid Checkout sessions using a `pi_...` payment reference. The staged subscription-receipt candidate (parent draft PR #5) instead uses `invoice:in_...` for paid invoices and ignores new subscription Checkout receipts. This correctly deduplicates **new** collections but, without further checks, a previously recorded legacy Checkout receipt and a subsequent paid invoice for the same PaymentIntent could appear as two separate paid rows.

This branch is a review-only incremental patch **on top of** PR #5. No live database migration, webhook change, payment attempt, production deployment, or customer notification was performed.

## Database changes (additive)

`payment_receipt_aliases` records the invoice reference, the older PaymentIntent reference, the signed event ID, an outcome label, invoice amount/currency and a timestamp. Existing `stripe_events` and `payments` rows are never deleted or rewritten.

When processing an `invoice.paid` event:
- If an alias for the same invoice is already persisted, suppress the redundant invoice payment row, including subsequent Event IDs that omit the PaymentIntent.
- If the invoice payload contains a legacy `payment_intent` or a modern paid `payments.data[].payment.payment_intent` that already has a row in `payments`, preserve that row and do **not** add `invoice:...`.
- If exactly one PaymentIntent matches, amount/currency agree, and the modern invoice payment list is complete, record `MATCHED_LEGACY_RECEIPT`. If amounts differ, multiple PaymentIntents are involved, or Stripe indicates a paginated/incomplete payment list, record `MANUAL_RECONCILIATION`.
- Never forward a reconciled duplicate invoice to automated fulfillment. Existing accepted payment notification remains the owner of the receipt.
- Aggregate counts of aliases are included **only** within the already bearer-protected revenue metrics endpoint; no customer email or payment reference is exposed by the new fields.

The distinct `invoice:in_...` records for genuinely separate renewals remain valid, including multiple invoice cycles for one subscription. The guard never assumes two equal-dollar invoices are the same payment.

## Mandatory release verification

1. Obtain explicit owner authorization for the exact staging/production release only **after** Google account recovery and 2FA are independently secured.
2. Review parent PR #3 security changes and PR #5 subscription dedupe. This branch must not be merged into `main` independently of them.
3. Run `python -m unittest discover -s tests -p 'test_*.py' -v` on GitHub Actions. Unit tests use isolated SQLite databases and deterministic Stripe-shaped fixtures; they are **not** live payment proof.
4. Before any production mutation, capture an application-consistent, encrypted SQLite backup from the **actual Railway attached volume**, using a safe read transaction or SQLite backup API. Verify backup restoration in staging; never copy a live WAL file as an uncoordinated substitute.
5. Record actual existing ledger counts and current invoice-to-PaymentIntent correlations with read-only, restricted access. **Known limitation:** if an older checkout row has already been double counted with an invoice row, or a Stripe invoice payload supplies no linked PaymentIntent, this patch cannot reconstruct the relationship automatically. Flag these for human audit; never erase rows or claim the issue resolved.
6. Test real Stripe-signed staging deliveries with unique event IDs, duplicate/reordered redelivery, modern and legacy invoice schemas, and an authenticated downstream fulfillment recipient. Check positive/negative tests and verify no duplicate downstream notification.
7. Check `MATCHED_LEGACY_RECEIPT` and `MANUAL_RECONCILIATION` alias counts using a private authenticated metrics request, then reconcile against the authoritative Stripe ledger before approving any monetary report. Stop if totals diverge.
8. Explicitly map the **six currently enabled** Stripe webhook destinations by owner and their side effects. Multiple listeners receiving the same event are legitimate, but they must not independently fulfill the same order or report independent revenue.
9. Prepare a tested, single-deployment rollback and SQLite schema compatibility plan. Additive schema changes remain on rollback; do not use `git reset --hard`, `sqlite3 ... DELETE`, or `kill -9` as rollback mechanisms.

## Strict status

- This patch closes a **specific historical alias edge case** under traceable identifiers and preserves evidence, not all possible duplicates.
- Security, genuine signed live event delivery, Google account ownership, Stripe webhook destination ownership, and customer fulfillment authorization remain separate gates.
- No production deployment, SMTP sending, Stripe mutation, financial success, or real buyer transaction has been claimed.
