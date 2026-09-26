# Garcar Payment Intake

Verified, idempotent Stripe webhook intake for Garcar's live storefront.

## Why this iteration exists

The earlier emergency receiver could accept unsigned webhooks when the signing secret was missing, stored events under `/tmp`, treated repeated Stripe events as separate payments, and mapped every `$497` payment to one offer even though the storefront has more than one `$497` SKU. This version closes those gaps.

## Production behavior

- Rejects webhooks unless `STRIPE_WEBHOOK_SECRET` is configured.
- Allows unsigned events only when `ALLOW_UNVERIFIED_WEBHOOKS=true` is explicitly set for local testing.
- Verifies Stripe `v1` signatures and rejects stale timestamps.
- Stores Stripe event IDs and payment references in SQLite with uniqueness constraints.
- Ignores duplicate deliveries and unpaid Checkout sessions.
- Uses Stripe metadata to disambiguate offers; an untagged `$497` purchase enters manual review.
- Supports an optional HTTPS fulfillment webhook.
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

Local unsigned testing only:

```env
ALLOW_UNVERIFIED_WEBHOOKS=true
```

Never enable that flag in production.

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
