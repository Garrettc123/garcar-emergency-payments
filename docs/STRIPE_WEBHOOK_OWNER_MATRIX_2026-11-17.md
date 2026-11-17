# Stripe live webhook ownership map — 2026-11-17

**Evidence boundary:** Read-only Stripe `GET /v1/webhook_endpoints` returned **six enabled** account destinations. Matching GitHub source and Railway live configuration verified **three** Railway services. Netlify and Grok runtime behavior was **not** verified. No webhook was disabled, repointed, replayed, or edited.

| Stripe endpoint ID | Destination | Enabled event scope | Verified processing | Release decision |
| --- | --- | --- | --- | --- |
| `we_1Uc9mvFKGbk21LK5UBsnHMO0` | `garcar-emergency-payments-production.up.railway.app/stripe-webhook` | checkout completed, delayed checkout success, invoice paid | **Receiver ledger**; production runs old `main`, persistent SQLite; `FULFILLMENT_WEBHOOK_URL` and token **not present among configured Railway variable names**. The `ALLOW_UNVERIFIED_WEBHOOKS` variable exists, but its *value* is redacted; signature-bypass state is unverified | Candidate PRs #3, #5 and #6 need independent staging tests and controlled deployment |
| `we_1UZ2dIFKGbk21LK5suhTmi2O` | `garcar-rhns-command-center-production.up.railway.app/api/stripe/webhook` | checkout completed, delayed checkout success, invoice paid, payout paid/failed | **Secondary signed event ledger** in `Garrettc123/garcar-command-center`; `applyStripeEvent` updates file-backed local state and audit chain, no outbound fulfillment call in this handler | Keep for observation only; label metrics local/non-authoritative, avoid duplicated aggregate sales reporting |
| `we_1UYztqFKGbk21LK5MeqgAL4b` | `web-production-d0e94.up.railway.app/webhooks/stripe` | checkout completed, delayed success/failure | **Signed, source-price-checked manual order queue** in `Garrettc123/tree-of-life-system`, stored in SQLite volume; endpoint explicitly does not mark orders fulfilled, charge, or send messages | Candidate first-party paid intake queue for MLS-497, but verify durable real signed delivery and buyer join before launch |
| `we_1UUUQyFKGbk21LK5LlDA0SAN` | `garcar-stripe-webhook.netlify.app/stripe-webhook` | checkout, invoices, PaymentIntents, subscriptions | **Unverified runtime** | Investigate owner, active code, raw signature verification, event replay protection and external side effects; do not disable blindly |
| `we_1UFirRFKGbk21LK5hSYT06Ws` | `apex-revenue-system.netlify.app/webhooks/stripe` | broad invoices, payments, subscriptions, schedules, entitlements | **Unverified runtime**; repository named `Garrettc123/apex-revenue-system` exists but no trustworthy live deployment-to-commit mapping was verified | Inspect Netlify deploy and side effects; reconcile against Stripe receipt / entitlements ownership |
| `we_1U78cEFKGbk21LK5aCSq15Sl` | `grok.com/connectors/v1/stripe-webhook/acct_1SS3dpFKGbk21LK5` | subscription created/deleted | **Provider connector; downstream semantics not verified** | Verify connector necessity and permissions through its provider before changing settings |

**Event fanout (one event can legitimately go to more than one distinct listener):**
- `checkout.session.completed`: **5 enabled endpoints**
- `checkout.session.async_payment_succeeded`: **3**
- `invoice.paid`: **4**
- `payment_intent.succeeded`: **2**
- `customer.subscription.created`: **3**

This proves overlapping delivery subscriptions, **not** duplicate actual charges. Per-endpoint event deduplication is not sufficient to certify globally unique fulfillment: choose **one** authoritative payment/entitlement record and **one** downstream fulfillment owner, while permitting read-only observers. A single production order must be mapped to its Stripe PaymentIntent, Checkout Session and, where applicable, Invoice ID to avoid counting the same collection twice.

## Release blockers

1. Google account recovery/2FA change still requires owner classification and sign-in security check before adjusting secrets or deployment permissions.
2. The emergency receiver's production `/health` returning 200 does not certify signed delivery, metrics authorization, or fulfillment; current deployed `main` does not contain the draft PR hardening.
3. Invoice/Checkout accounting migrations require a real attached-volume backup, rollback rehearsal, and reconciliation of pre-existing legacy paid rows before PRs #3/#5/#6 can be promoted.
4. Netlify and Grok endpoint behavior is unverified; do not disable or edit them solely to reduce overlapping event subscriptions.
5. Confirm actual Stripe-generated **signed** staging delivery, durable event receipt, repeated/reordered delivery, authenticated order intake and manual approval before any production change.

**No Stripe write, production deployment, live charge, customer message or existing ledger modification was performed for this audit.**
