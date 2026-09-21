# Garcar Emergency Payments — Activate in <15 minutes

The Cloudflare Worker (`garcar-payments.garrettc123.workers.dev`) does not resolve.
Vercel returns 402 DEPLOYMENT_DISABLED.
Both are billing-locked. This package **bypasses them completely**.

## What this does
- Receives Stripe webhooks (`checkout.session.completed`, etc.)
- Verifies signature (when secret is set)
- Logs every real payment with customer email + amount + offer
- Writes a durable JSONL record
- Returns 200 so Stripe stops retrying
- Exposes `/health` and `/mrr`

It does **not** require Supabase, HubSpot, Asana, Notion, or any of the locked secrets.

## 1. Deploy (pick one free host)

### Option A — Railway (recommended, free tier)
```bash
# Install Railway CLI once
npm i -g @railway/cli
railway login
railway init
railway up
# Then set secrets in Railway dashboard:
# STRIPE_WEBHOOK_SECRET=whsec_...
# FULFILLMENT_EMAIL=gwc2780@gmail.com
```
Copy the public URL Railway gives you (e.g. `https://garcar-emergency-XXXX.up.railway.app`).

### Option B — Render free web service
- New → Web Service → connect this folder or push to a new public repo
- Build: `pip install -r requirements.txt` (empty is fine)
- Start: `python main.py`
- Add env vars as above

### Option C — Any $5 VPS / Fly.io / even a home server
```bash
docker build -t garcar-emergency .
docker run -d -p 8080:8080 \
  -e STRIPE_WEBHOOK_SECRET=whsec_... \
  -e FULFILLMENT_EMAIL=gwc2780@gmail.com \
  garcar-emergency
```

## 2. Point Stripe at the new endpoint (critical)

1. Go to https://dashboard.stripe.com/webhooks
2. Add endpoint → URL = `https://YOUR-NEW-HOST/stripe-webhook`
3. Events to send:
   - `checkout.session.completed`
   - `payment_intent.succeeded`
   - `invoice.paid`
4. Copy the **Signing secret** (`whsec_...`) → put it in the host’s env as `STRIPE_WEBHOOK_SECRET`
5. (Optional) Disable or leave the old dead Cloudflare/Vercel endpoints; they no longer matter.

## 3. Test without real money
```bash
# In one terminal (with Stripe CLI)
stripe listen --forward-to localhost:8080/stripe-webhook

# In another
stripe trigger checkout.session.completed
```
You should see the “🔥 REAL MONEY RECEIVED” log (even for test events).

## 4. Live offers that already work
These Stripe Payment Links are healthy and charge real cards **right now**:

| Offer | Link |
|-------|------|
| $497 Lead List Review | https://buy.stripe.com/8x2eVddjf0hQ86Tf0f43S2h |
| $2,500 Install | https://buy.stripe.com/6oUaEX4MJ0hQevhf0f43S2i |

After a real payment the emergency service will log the customer email. Fulfill from that email within the SLA (48 h for $497, 72 h for $2,500).

## 5. Force demand (the other half of “fix all”)
While the webhook is being pointed, run outreach. The storefront is already live at https://garrettc123.github.io/

Copy-paste ready message (DFW real-estate team leads):

```
Hey [Name] — Garrett in Grandview / Johnson County.

Quick question: when a new Zillow or website lead hits and the assigned agent is already in a showing, who calls that person back?

Most teams have no named backup and no timer. The lead goes cold.

I do a $497 review of one recent list (last 30 days, one source). You get a marked spreadsheet + one-page count + written rules you can use the same day. No new CRM.

If the map is wrong you say so.

Link if you want to start: https://buy.stripe.com/8x2eVddjf0hQ86Tf0f43S2h
Or just reply “497” and I’ll send it.
```

Send 30–50 of these today. Every “start” gets the live link the same hour.

## Status after activation
- Money can land → webhook fires → you get an email + log → you fulfill.
- Old locked Cloudflare + Vercel stay dead; they are no longer in the critical path.
- Architecture, agents, BioForge modules can resume in parallel. First dollar no longer blocked by them.
