# Garcar Payment Intake

Verified, idempotent Stripe webhook intake for Garcar's live storefront.

## Why this iteration exists

The earlier emergency receiver could accept unsigned webhooks when the signing secret was missing, stored events under `/tmp`, treated repeated Stripe events as separate payments, and mapped every `$497` payment to one offer even though the storefront has more than one `$497` SKU. This version closes those gaps.

## Production behavior

- Rejects webhooks unless `STRIPE_WEBHOOK_SECRET` is configured.
- Always rejects unsigned webhooks, even when a legacy testing flag is set.
- Verifies Stripe `v1` signatures and rejects stale timestamps.
- Stores Stripe event IDs and payment references in SQLite with uniqueness constraints.
- Ignores duplicate deliveries and unpaid Checkout sessions.
- Uses Stripe metadata to disambiguate offers; an untagged `$497` purchase enters manual review.
- Supports an optional HTTPS fulfillment webhook only with a configured bearer authentication token.
- Reports collected amounts without mislabeling them as authoritative MRR.

## Required environment

```env
STRIPE_WEBHOOK_SECRET=whsec_...
DATA_DIR=/data
```

Use a persistent volume mounted at `/data`. Ephemeral filesystems are not suitable for payment records.

Optional fulfillment notification:

```env
FULFILLMENT_WEBHOOK_URL=https://your-secure-automation.example/webhook
FULFILLMENT_WEBHOOK_TOKEN=replace-with-a-long-secret
```

All tests, including local development, must use correctly signed Stripe test events. The old `ALLOW_UNVERIFIED_WEBHOOKS` flag is ignored by the code.

## Run

```bash
python main.py
```

## Test

```bash
python -m unittest discover -s tests -v
```

Then test the full Stripe path:

```bash
stripe listen --forward-to localhost:8080/stripe-webhook
# Copy the whsec value printed by Stripe CLI into STRIPE_WEBHOOK_SECRET.
stripe trigger checkout.session.completed
```

## Stripe configuration

Create one webhook endpoint at:

```text
https://YOUR-HOST/stripe-webhook
```

Subscribe to:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `invoice.paid`

Add metadata to every Payment Link or Checkout Session:

```text
sku=contractor-audit-47
sku=workflow-sprint-497
sku=lead-list-review-497
sku=crm-safeguard-2500
```

Also set `offer_name` and `fulfill_hours` when the offer differs from the amount fallback.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness and configuration posture |
| GET | `/revenue` | Recorded payment totals; not accounting-grade MRR |
| GET | `/mrr` | Backward-compatible alias for `/revenue` |
| POST | `/stripe-webhook` | Verified Stripe event intake |

## Deployment gate

Do not send paid traffic until all of these pass:

1. Persistent storage is mounted and survives a restart.
2. `STRIPE_WEBHOOK_SECRET` is configured.
3. A Stripe CLI test event returns HTTP 200.
4. Replaying the same event returns `duplicate: true` and does not increase totals.
5. Each live Payment Link contains a distinct `sku` metadata value.
6. The fulfillment notification reaches a monitored system.

## Founder

Garrett Carrol · Grandview / Kopperl, Texas

## Readiness and private metrics (security hardening)

- GET `/health` is public liveness and does not expose storage paths or revenue.
- GET `/readyz` returns HTTP 503 until the signed-webhook receiver and database are ready; configure Railway's healthcheck path to `/readyz` after rollout.
- GET `/revenue` and `/mrr` now require `Authorization: Bearer <METRICS_READ_TOKEN>`; without the variable the endpoint fails closed with HTTP 503.
- Unsigned webhook intake is prohibited in every environment. Use the test signing secret and Stripe CLI or locally signed test fixtures.
- Cross-currency totals appear in `by_currency`. A global minor-unit gross is `null` when multiple currencies exist.

Before merging or deploying this change, provision a distinct, strong `METRICS_READ_TOKEN` on the payment receiver and update any authorized metrics consumers. Do not log or share its value. This PR does not alter the currently deployed production endpoint.


## Crash-safe notification handoff (draft PR #3, NOT deployed)

Verified signed, live-mode, paid Stripe events are persisted before any downstream notification. A SKU must be uniquely attributable. Unknown or name-only metadata is stored as `PAID_MANUAL_REVIEW` and never automatically dispatched.

When `FULFILLMENT_WEBHOOK_URL` is configured, an eligible payment obtains a 120-second SQLite lease with a random claim token. A second concurrent worker cannot claim it. Expired leases can be reacquired on Stripe's subsequent signed redelivery; stale workers cannot overwrite the new owner's claim. HTTP 2xx is stored as `NOTIFICATION_ACCEPTED`, **not** as a claim that the purchased work or digital deliverable has been fulfilled.

Non-2xx responses and network failures return the record to `PAID_PENDING_FULFILLMENT`. After 12 delivery attempts it moves to `DELIVERY_MANUAL_INTERVENTION` for operator reconciliation. Legacy `FULFILLMENT_NOTIFIED` rows from earlier versions are moved to `DELIVERY_RECONCILIATION_REQUIRED` because the old implementation set the state before receiving downstream HTTP acceptance; they are never blindly replayed.

No background outbound notification worker is enabled by this change. Recovery relies on Stripe's retries, and an operator must reconcile events when Stripe's retry window has elapsed. The downstream system must enforce the stable `Idempotency-Key` header and **cannot** send customer communications, charge customers, or mark final delivery complete without its own approval and evidence gates.

**Actual production posture observed November 16, 2026:** Railway mounts `/data` and has `STRIPE_WEBHOOK_SECRET` listed, but `FULFILLMENT_WEBHOOK_URL` and `METRICS_READ_TOKEN` were not in the receiver's variable names. Therefore downstream fulfillment is *not configured*, and a production merge without first provisioning private metrics access and validating compatibility would be unsafe.

### Independent release verification
Run on a reviewed checkout of the exact PR commit SHA, with real Python 3.12:
```bash
python -m py_compile main.py tests/test_main.py
python -m unittest discover -s tests -p 'test_*.py' -v
```
Do not treat a GitHub Actions job with zero recorded steps as a passing test. Before deploying, validate a **signed Stripe test-mode event** against an isolated test receiver, verify duplicate handling and crash recovery, then test a legitimate live checkout and delivery receipt under operator approval. Never count a synthetic test event as real revenue.
