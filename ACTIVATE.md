# Garcar Payments — Production Activation

This service records verified Stripe events and queues paid work for fulfillment. It does not create demand, send marketing messages, or complete client delivery by itself.

## Deploy

Deploy the repository branch to Railway, Render, Fly.io, or a VPS. Mount a persistent volume at `/data`, set `DATA_DIR=/data`, and expose the platform-provided `PORT`.

**Critical:** The container runs as `nobody`. After mounting the volume, ensure the mounted `/data` directory is writable by the container user (uid of `nobody`, typically 65534). Verify write access both immediately after mount and after a container restart. Example (host side before start):

```bash
mkdir -p /path/to/host/data
chown -R 65534:65534 /path/to/host/data
# then mount /path/to/host/data -> /data
```

If the platform hides the image-prepared directory behind a non-writable mount, SQLite init will fail and the service will not start.

Set these production variables:

```env
STRIPE_WEBHOOK_SECRET=whsec_...
DATA_DIR=/data
FULFILLMENT_WEBHOOK_URL=https://YOUR-MONITORED-ENDPOINT
FULFILLMENT_WEBHOOK_TOKEN=YOUR-LONG-RANDOM-TOKEN
```

`ALLOW_UNVERIFIED_WEBHOOKS` must be absent or `false`.

## Connect Stripe

In the Stripe Dashboard, create one endpoint at:

```text
https://YOUR-HOST/stripe-webhook
```

Subscribe to `checkout.session.completed`, `checkout.session.async_payment_succeeded`, and `invoice.paid`. Copy Stripe's endpoint signing secret into `STRIPE_WEBHOOK_SECRET` and restart the service.

## Tag offers

The storefront currently exposes multiple offers, including more than one `$497` purchase. Amount alone is not enough to identify the buyer's deliverable. Add distinct metadata to every Payment Link:

| Offer | Required `sku` | Suggested SLA |
|---|---|---|
| $47 Contractor Lead Leak Audit | `contractor-audit-47` | 48 hours |
| $497/week Workflow Sprint | `workflow-sprint-497` | Define in signed scope |
| $497 Real Estate Lead-List Review | `lead-list-review-497` | 48 hours |
| $2,500 CRM Safeguard | `crm-safeguard-2500` | 72 hours |

Recommended metadata fields are `sku`, `offer_name`, and `fulfill_hours`.

## Prove the payment loop

Run the unit tests, then use Stripe CLI against the deployed endpoint. Confirm one event creates one payment, a replay is marked duplicate, the payment remains after a restart, and the fulfillment destination receives the alert.

## Operating rule

Do not report forecasts, generated test transactions, or Checkout links as revenue. Revenue is recognized here only when a verified paid Stripe event is recorded. Use Stripe's own reports for accounting and subscription MRR.
