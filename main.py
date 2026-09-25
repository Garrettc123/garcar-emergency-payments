"""Garcar Stripe webhook receiver with verified, idempotent payment intake."""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

PORT = int(os.environ.get("PORT", "8080"))
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
ALLOW_UNVERIFIED_WEBHOOKS = os.environ.get("ALLOW_UNVERIFIED_WEBHOOKS", "false").lower() == "true"
FULFILLMENT_WEBHOOK_URL = os.environ.get("FULFILLMENT_WEBHOOK_URL", "")
FULFILLMENT_WEBHOOK_TOKEN = os.environ.get("FULFILLMENT_WEBHOOK_TOKEN", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DB_PATH = Path(os.environ.get("DB_PATH", str(DATA_DIR / "garcar_payments.sqlite3")))
SIGNATURE_TOLERANCE_SECONDS = int(os.environ.get("STRIPE_SIGNATURE_TOLERANCE_SECONDS", "300"))

OFFER_BY_AMOUNT = {
    4700: {"name": "$47 Contractor Lead Leak Audit", "sku": "contractor-audit-47", "fulfill_hours": 48},
    250000: {"name": "$2,500 CRM Safeguard", "sku": "crm-safeguard-2500", "fulfill_hours": 72},
}
FULFILLABLE_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "invoice.paid",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{utc_now()}] {message}", flush=True)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_db() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS stripe_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                payment_reference TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                customer_email TEXT,
                amount_minor INTEGER NOT NULL,
                currency TEXT NOT NULL,
                offer_name TEXT NOT NULL,
                sku TEXT NOT NULL,
                fulfill_hours INTEGER NOT NULL,
                session_id TEXT,
                recurring INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES stripe_events(event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments(created_at);
            """
        )


def verify_stripe_signature(payload: bytes, signature_header: str, secret: str, now: int | None = None) -> bool:
    if not secret or not signature_header:
        return False
    timestamp = None
    signatures = []
    for part in signature_header.split(","):
        key, separator, value = part.partition("=")
        if not separator:
            continue
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if not timestamp or not signatures:
        return False
    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    current_time = int(time.time()) if now is None else now
    if abs(current_time - signed_at) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _metadata(obj: dict) -> dict:
    raw = obj.get("metadata") or {}
    return raw if isinstance(raw, dict) else {}


def resolve_offer(obj: dict, amount_minor: int) -> dict:
    metadata = _metadata(obj)
    sku = str(metadata.get("sku") or metadata.get("offer_id") or "").strip()
    name = str(metadata.get("offer_name") or metadata.get("product_name") or "").strip()
    hours = _safe_int(metadata.get("fulfill_hours"), 0)
    if sku or name:
        return {
            "sku": sku or "metadata-offer",
            "name": name or sku,
            "fulfill_hours": hours if 1 <= hours <= 720 else 48,
        }
    if amount_minor in OFFER_BY_AMOUNT:
        return OFFER_BY_AMOUNT[amount_minor]
    if amount_minor == 49700:
        return {
            "name": "$497 purchase — verify source link before fulfillment",
            "sku": "manual-review-497",
            "fulfill_hours": 48,
        }
    return {
        "name": f"Unmapped payment ({amount_minor} minor units)",
        "sku": "manual-review-unmapped",
        "fulfill_hours": 48,
    }


def extract_payment(event: dict) -> dict | None:
    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("type") or "").strip()
    obj = ((event.get("data") or {}).get("object") or {})
    if not event_id or event_type not in FULFILLABLE_EVENTS or not isinstance(obj, dict):
        return None
    if event_type.startswith("checkout.session") and obj.get("payment_status") not in (None, "paid", "no_payment_required"):
        return None

    amount_minor = _safe_int(
        obj.get("amount_total", obj.get("amount_paid", obj.get("amount_received", obj.get("amount_due", 0))))
    )
    currency = str(obj.get("currency") or "usd").lower()
    customer_details = obj.get("customer_details") or {}
    customer_email = customer_details.get("email") or obj.get("customer_email") or obj.get("receipt_email") or ""
    offer = resolve_offer(obj, amount_minor)
    reference = str(
        obj.get("payment_intent")
        or obj.get("charge")
        or (obj.get("subscription") and f"{obj.get('subscription')}:{obj.get('id')}")
        or obj.get("id")
        or event_id
    )
    recurring = event_type == "invoice.paid" or obj.get("mode") == "subscription"
    return {
        "payment_reference": reference,
        "event_id": event_id,
        "event_type": event_type,
        "customer_email": customer_email,
        "amount_minor": amount_minor,
        "currency": currency,
        "offer_name": offer["name"],
        "sku": offer["sku"],
        "fulfill_hours": offer["fulfill_hours"],
        "session_id": str(obj.get("id") or ""),
        "recurring": 1 if recurring else 0,
        "status": "PAID_PENDING_FULFILLMENT",
        "created_at": utc_now(),
        "metadata_json": json.dumps(_metadata(obj), sort_keys=True),
    }


def record_event(event: dict, payload: bytes) -> tuple[bool, dict | None]:
    event_id = str(event.get("id") or "").strip()
    event_type = str(event.get("type") or "").strip()
    if not event_id or not event_type:
        raise ValueError("Stripe event must include id and type")
    payment = extract_payment(event)
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO stripe_events(event_id, event_type, received_at, payload_sha256) VALUES (?, ?, ?, ?)",
            (event_id, event_type, utc_now(), hashlib.sha256(payload).hexdigest()),
        )
        if cursor.rowcount == 0:
            return False, None
        if payment:
            connection.execute(
                """
                INSERT OR IGNORE INTO payments(
                    payment_reference, event_id, event_type, customer_email, amount_minor,
                    currency, offer_name, sku, fulfill_hours, session_id, recurring,
                    status, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(payment[key] for key in (
                    "payment_reference", "event_id", "event_type", "customer_email", "amount_minor",
                    "currency", "offer_name", "sku", "fulfill_hours", "session_id", "recurring",
                    "status", "created_at", "metadata_json"
                )),
            )
    return True, payment


def notify_fulfillment(payment: dict) -> bool:
    if not FULFILLMENT_WEBHOOK_URL:
        return False
    if not FULFILLMENT_WEBHOOK_URL.startswith("https://"):
        log("FULFILLMENT_WEBHOOK_URL rejected: HTTPS is required")
        return False
    payload = {
        "event": "garcar.payment.received",
        "payment_reference": payment["payment_reference"],
        "customer_email": payment["customer_email"],
        "amount_minor": payment["amount_minor"],
        "currency": payment["currency"],
        "offer_name": payment["offer_name"],
        "sku": payment["sku"],
        "fulfill_hours": payment["fulfill_hours"],
        "created_at": payment["created_at"],
    }
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "garcar-payments/2.0"}
    if FULFILLMENT_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {FULFILLMENT_WEBHOOK_TOKEN}"
    try:
        with urlopen(Request(FULFILLMENT_WEBHOOK_URL, data=body, headers=headers, method="POST"), timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        log(f"Fulfillment notification failed: {type(exc).__name__}")
        return False


def aggregate_revenue() -> dict:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS paid_events,
                   COALESCE(SUM(amount_minor), 0) AS gross_minor,
                   COALESCE(SUM(CASE WHEN recurring = 1 THEN amount_minor ELSE 0 END), 0) AS recurring_collections_minor
            FROM payments
            """
        ).fetchone()
    return {
        "paid_events": row["paid_events"],
        "gross_collected_minor": row["gross_minor"],
        "recurring_collections_minor": row["recurring_collections_minor"],
        "currency_note": "Amounts are stored in each payment's currency; do not combine mixed currencies for accounting.",
        "mrr_note": "Recurring collections are not normalized MRR. Use Stripe Billing for authoritative MRR.",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "GarcarPayments/2.0"

    def log_message(self, format_string, *args):
        log(f"HTTP {self.address_string()} {format_string % args}")

    def _json(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/health", "/livez", "/readyz"):
            self._json(200, {
                "status": "ok",
                "service": "garcar-emergency-payments",
                "version": "2.0",
                "webhook_verification_required": not ALLOW_UNVERIFIED_WEBHOOKS,
                "database": str(DB_PATH),
                "time": utc_now(),
            })
        elif path in ("/revenue", "/mrr"):
            self._json(200, aggregate_revenue())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/stripe-webhook":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid content length"})
            return
        if length <= 0 or length > 2_000_000:
            self._json(413, {"error": "invalid payload size"})
            return
        payload = self.rfile.read(length)
        signature = self.headers.get("Stripe-Signature", "")
        if STRIPE_WEBHOOK_SECRET:
            if not verify_stripe_signature(payload, signature, STRIPE_WEBHOOK_SECRET):
                self._json(400, {"error": "invalid signature"})
                return
        elif not ALLOW_UNVERIFIED_WEBHOOKS:
            log("Webhook rejected: STRIPE_WEBHOOK_SECRET is not configured")
            self._json(503, {"error": "webhook verification not configured"})
            return
        else:
            log("WARNING: accepting unverified webhook because ALLOW_UNVERIFIED_WEBHOOKS=true")
        try:
            event = json.loads(payload)
            inserted, payment = record_event(event, payload)
        except (json.JSONDecodeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        except sqlite3.Error:
            log("Database write failed")
            self._json(500, {"error": "database unavailable"})
            return
        if not inserted:
            self._json(200, {"received": True, "duplicate": True})
            return
        notified = notify_fulfillment(payment) if payment else False
        if payment:
            log(f"PAYMENT QUEUED reference={payment['payment_reference']} sku={payment['sku']} amount_minor={payment['amount_minor']}")
        self._json(200, {
            "received": True,
            "payment_queued": bool(payment),
            "fulfillment_notified": notified,
        })


def main():
    init_db()
    if not STRIPE_WEBHOOK_SECRET and not ALLOW_UNVERIFIED_WEBHOOKS:
        log("STRIPE_WEBHOOK_SECRET missing: health checks pass, but webhooks are rejected")
    log(f"Garcar Payments v2 listening on 0.0.0.0:{PORT}; database={DB_PATH}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
