# Garcar Emergency Payments

**Bypasses the locked Cloudflare Workers + Vercel control plane.**

Zero-dependency Stripe webhook receiver + fulfillment logger.

## Live status
- Old Worker DNS: dead
- Vercel: 402 DEPLOYMENT_DISABLED
- This service: ready to deploy anywhere free

## Quick start
See [ACTIVATE.md](ACTIVATE.md)

## Outreach
See [outreach_batch_1.txt](outreach_batch_1.txt)

## Endpoints
| Method | Path | Purpose |
|--------|------|--------|
| GET | /health | Liveness |
| GET | /mrr | Paid event count |
| POST | /stripe-webhook | Stripe events |

## Founder
Garrett Carrol · Grandview / Kopperl, TX
