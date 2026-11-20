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

### Reconcile a $497 sandbox checkout

Run `stripe listen` with the Garcar sandbox selected in the Stripe CLI. Forward only completed Checkout Sessions to the separate loopback receiver.

```bash
stripe listen --events checkout.session.completed --forward-to http://127.0.0.1:4242/stripe-webhook
```

In a second terminal, set `STRIPE_SANDBOX_WEBHOOK_SECRET` privately to that listener's signing secret, then run:

```bash
python sandbox_receiver.py --data-dir "$HOME/.local/share/garcar/stripe-sandbox"
```

Use the Garcar sandbox to create a one-time Checkout Session for USD 497.00 with session metadata `sku=MLS-497`. Complete its hosted checkout with a Stripe test payment method. In the sandbox's **Workbench → Events**, open the corresponding `checkout.session.completed` event and copy its Checkout Session ID. Set `SESSION_ID` to that value in your shell.

```bash
stripe checkout sessions retrieve "$SESSION_ID" |
  python sandbox_receiver.py --data-dir "$HOME/.local/share/garcar/stripe-sandbox" --verify-session
```

A successful comparison prints the session, PaymentIntent, event ID, payload digest, matching delivery count and receipt count. It checks the amount, currency and SKU against the stored receipt.

Restart the sandbox receiver using the same directory. To exercise a Stripe-initiated retry, use **Resend** on the original event at a registered sandbox event destination routed to this receiver. Repeat the comparison with `--verify-session --require-redelivery`. It succeeds only when the ledger contains one matching receipt and at least two deliveries with the same event ID and payload digest. Existing ledger events start at one known delivery when this counter is introduced.

The local test suite exercises this check with a signed fixture and a process restart. A provider acceptance run also requires a Checkout Session and delivery history from the selected Stripe sandbox.

The sandbox entry point uses a dedicated SQLite file, binds to loopback and disables fulfillment. Its local receipt count is excluded from the production ledger.

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

## Webhook and fulfillment failure boundaries

The receiver distinguishes liveness from signature readiness:
- GET /health reports process liveness, signing posture and fulfillment
  configuration without exposing the SQLite internal path.
- GET /ready returns HTTP 503 if the Stripe signing secret is missing.
- GET /fulfillment-status returns aggregate counts of pending, attempting,
  unknown-outcome and notified payments, without customer identifiers.

Production webhook events always require verified Stripe signatures.
The flag ALLOW_UNVERIFIED_WEBHOOKS=true has no effect on a production listener.
For deliberate unsigned testing, ALL of these must be set:

    GARCAR_ENVIRONMENT=local HOST=127.0.0.1 ALLOW_UNVERIFIED_WEBHOOKS=true python main.py

Never deploy that local-only invocation to a public endpoint.

**Ambiguous fulfillment outcomes:** a downstream receiver may execute an
action but lose its response. Blind replay can cause duplicate delivery.
The receiver reserves an attempt in SQLite; it marks FULFILLMENT_NOTIFIED
only after confirmed 2xx. A timeout/non-2xx becomes
FULFILLMENT_OUTCOME_UNKNOWN; a crash during dispatch remains
FULFILLMENT_ATTEMPTING. Both require human reconciliation with the
downstream idempotency key before a retry. Duplicate Stripe events do not
auto-dispatch these uncertain states.

This favors fail-closed delivery over unverified duplicate fulfillment,
but does not imply every valid payment was fulfilled. Maintain a monitored
reconciliation queue and an idempotent fulfillment target.

**Release boundary:** this PR changes source code and local tests only.
No live Stripe endpoints, deployment, secrets, or fulfillment target were
altered. Verify signature configuration, delivery logs and the
persisted /data volume before routing paid traffic.

## Deployment gate

Do not send paid traffic until all of these pass:

1. Persistent storage is mounted and survives a restart.
2. `STRIPE_WEBHOOK_SECRET` is configured.
3. A paid Stripe sandbox checkout reconciles to one receipt in the sandbox ledger.
4. Redelivery of the same event returns `duplicate: true`; the original session still reconciles to one receipt.
5. Each live Payment Link contains a distinct `sku` metadata value.
6. The fulfillment notification reaches a monitored system.
7. Authorized metrics clients have the configured `METRICS_READ_TOKEN`, and the downstream fulfillment receiver verifies its own bearer token.

## Founder

Garrett Carrol · Grandview / Kopperl, Texas
