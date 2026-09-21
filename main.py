"""
Garcar Emergency Payments — zero-dependency Stripe webhook + fulfillment receiver.
Bypasses locked Cloudflare Workers + Vercel entirely.
Deploy to any free host (Railway, Render, Fly, even a $5 VPS).
"""

import os
import json
import hmac
import hashlib
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Optional: only import stripe if key is present (keeps cold-start tiny)
try:
    import stripe
except ImportError:
    stripe = None

PORT = int(os.environ.get("PORT", 8080))
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
FULFILLMENT_EMAIL = os.environ.get("FULFILLMENT_EMAIL", "gwc2780@gmail.com")
LOG_FILE = os.environ.get("LOG_FILE", "/tmp/garcar_payments.log")

# Live offer map (from storefront)
OFFERS = {
    "price_497": {"name": "$497 Lead List Review", "sku": "497-review", "fulfill_hours": 48},
    "price_2500": {"name": "$2,500 Install", "sku": "2500-install", "fulfill_hours": 72},
    # Fallback for orphan $47 link
    "price_47": {"name": "$47 Contractor Audit (orphan)", "sku": "47-audit", "fulfill_hours": 48},
}


def log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}\n"
    print(line, end="", flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass


def verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """Minimal Stripe signature verification (no SDK required)."""
    if not secret or not sig_header:
        return False
    try:
        elements = dict(item.split("=", 1) for item in sig_header.split(","))
        timestamp = elements.get("t")
        signature = elements.get("v1")
        if not timestamp or not signature:
            return False
        # Reject if older than 5 minutes
        if abs(time.time() - int(timestamp)) > 300:
            return False
        signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
        expected = hmac.new(
            secret.encode("utf-8"),
            signed_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        log(f"Signature verify error: {e}")
        return False


def extract_offer(session: dict) -> dict:
    """Map Stripe session / line items to a known offer."""
    amount = session.get("amount_total") or 0
    if amount == 49700:
        return OFFERS["price_497"]
    if amount == 250000:
        return OFFERS["price_2500"]
    if amount == 4700:
        return OFFERS["price_47"]
    return {
        "name": f"Unknown (${amount/100:.2f})",
        "sku": "unknown",
        "fulfill_hours": 48,
    }


def fulfill(event_type: str, data: dict):
    """Core fulfillment: log + prepare human hand-off."""
    session = data.get("object", {})
    customer_email = (
        session.get("customer_details", {}).get("email")
        or session.get("customer_email")
        or "unknown"
    )
    offer = extract_offer(session)
    payment_intent = session.get("payment_intent") or session.get("id")
    amount = (session.get("amount_total") or 0) / 100

    record = {
        "event": event_type,
        "time": datetime.now(timezone.utc).isoformat(),
        "customer_email": customer_email,
        "amount_usd": amount,
        "offer": offer["name"],
        "sku": offer["sku"],
        "fulfill_by_hours": offer["fulfill_hours"],
        "payment_intent": payment_intent,
        "session_id": session.get("id"),
        "status": "PAID — FULFILL NOW",
    }

    log("=" * 60)
    log("🔥 REAL MONEY RECEIVED")
    log(json.dumps(record, indent=2))
    log(f"ACTION: Email {FULFILLMENT_EMAIL} with lead list request + start the {offer['fulfill_hours']}h clock")
    log("=" * 60)

    # Persist for later systems
    try:
        with open("/tmp/garcar_paid_events.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

    return record


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log(f"HTTP {args[0]}")

    def _json(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health", "/livez", "/readyz"):
            self._json(200, {
                "status": "ok",
                "service": "garcar-emergency-payments",
                "mode": "bypass-locked-cloudflare-vercel",
                "offers_live": ["$497 review", "$2500 install"],
                "webhook": "/stripe-webhook",
                "time": datetime.now(timezone.utc).isoformat(),
            })
        elif path == "/mrr":
            # Minimal: count paid events from log
            count = 0
            total = 0.0
            try:
                with open("/tmp/garcar_paid_events.jsonl") as f:
                    for line in f:
                        rec = json.loads(line)
                        count += 1
                        total += rec.get("amount_usd", 0)
            except Exception:
                pass
            self._json(200, {"paid_events": count, "total_usd": total, "note": "emergency log only"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/stripe-webhook":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        sig = self.headers.get("Stripe-Signature", "")

        # Verify if secret is set; otherwise accept (emergency mode) but log warning
        if STRIPE_WEBHOOK_SECRET:
            if not verify_stripe_signature(payload, sig, STRIPE_WEBHOOK_SECRET):
                log("REJECTED: invalid Stripe signature")
                self._json(400, {"error": "invalid signature"})
                return
        else:
            log("WARNING: STRIPE_WEBHOOK_SECRET not set — accepting without verify (emergency)")

        try:
            event = json.loads(payload)
        except Exception:
            self._json(400, {"error": "bad json"})
            return

        event_type = event.get("type", "")
        data = event.get("data", {})

        log(f"Received event: {event_type}")

        if event_type in (
            "checkout.session.completed",
            "payment_intent.succeeded",
            "invoice.paid",
        ):
            record = fulfill(event_type, data)
            self._json(200, {"received": True, "fulfilled": record["sku"]})
        else:
            # Ack everything else so Stripe stops retrying
            self._json(200, {"received": True, "ignored": event_type})


def main():
    log("Garcar Emergency Payments starting")
    log(f"PORT={PORT}  webhook_secret_set={bool(STRIPE_WEBHOOK_SECRET)}")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
