# Garcar Payments — Production Activation

This service records verified Stripe events and queues paid work for fulfillment. It does not create demand, send marketing messages, or complete client delivery by itself.

## Deploy

Deploy the repository branch to Railway, Render, Fly.io, or a VPS. Mount a persistent volume at `/data`, set `DATA_DIR=/data`, and expose the platform-provided `PORT`.

**Critical:** The container runs as `nobody`. After mounting the volume, ensure `/data` is writable by uid 65534.

```env
STRIPE_WEBHOOK_SECRET=whsec_...
DATA_DIR=/data
FULFILLMENT_WEBHOOK_URL=https://YOUR-MONITORED-ENDPOINT
FULFILLMENT_WEBHOOK_TOKEN=YOUR-LONG-RANDOM-TOKEN
```

`ALLOW_UNVERIFIED_WEBHOOKS` must be absent or `false`.

## Connect Stripe

Endpoint:

```text
https://YOUR-HOST/stripe-webhook
```

Subscribe to `checkout.session.completed`, `checkout.session.async_payment_succeeded`, and `invoice.paid`.

## Tag offers (live 2026-09-27)

| Offer | sku | Link | SLA |
|---|---|---|---|
| Lead Leak Audit | LLA-47 | https://buy.stripe.com/3cI00j7YV0hQgDp8BR43S2v | 48 hours |
| Mark the leads that sat | MLS-497 | https://buy.stripe.com/8x2eVddjf0hQ86Tf0f43S2h | 48 hours |
| Callback clock install | LB-2500 | https://buy.stripe.com/14AdR95QN3u2af14lB43S2j | 3 business days after inputs |

Required metadata on every Payment Link: `sku`, `offer_name`, `fulfill_hours`.

Primary wedge until 1 Oct 2026: **LLA-47** against Google LSA missed-call billing.

## Prove the payment loop

One verified paid event = one payment row. Replays are duplicates. Restarts keep the row. Fulfillment URL gets the alert.

Do not report test cards, 4242, or Checkout-link clicks as revenue.
