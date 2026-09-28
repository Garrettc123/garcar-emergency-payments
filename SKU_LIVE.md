# Live SKUs — 2026-09-27

Primary cash path:

| sku | amount | checkout |
|---|---|---|
| LLA-47 | 4700 | https://buy.stripe.com/3cI00j7YV0hQgDp8BR43S2v |
| MLS-497 | 49700 | https://buy.stripe.com/8x2eVddjf0hQ86Tf0f43S2h |
| LB-2500 | 250000 | https://buy.stripe.com/14AdR95QN3u2af14lB43S2j |

On `checkout.session.completed`:
- persist event id
- POST fulfillment webhook if configured
- never treat 4242 live-mode declines as revenue

Legacy link still on some pages: `dRm8wPbb72pY2Mz8BR43S1D` — same $47 offer family. Prefer LLA-47.
