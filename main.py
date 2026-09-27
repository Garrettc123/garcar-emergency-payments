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
        return OFFER_BY_AMOUNT[amount_minor].copy()
    if amount_minor == 49700:
        return {
            "sku": "manual-review-497",
            "name": "$497 offer (manual review — add sku metadata)",
            "fulfill_hours": 48,
        }
    return {
        "sku": f"amount-{amount_minor}",
        "name": f"Unmapped amount {amount_minor}",
        "fulfill_hours": 48,
    }


def extract_payment(event: dict) -> dict | None:
    event_type = str(event.get("type") or "")
    if event_type not in FULFILLABLE_EVENTS:
        return None
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return None
    if event_type.startswith("checkout.session"):
        if str(obj.get("payment_status") or "").lower() != "paid":
            return None
        amount_minor = _safe_int(obj.get("amount_total"))
        currency = str(obj.get("currency") or "usd").lower()
        customer_email = ((obj.get("customer_details") or {}).get("email") or obj.get("customer_email") or "")
        payment_intent = str(obj.get("payment_intent") or obj.get("id") or "")
        recurring = False
    else:  # invoice.paid
        amount_minor = _safe_int(obj.get("amount_paid"))
        currency = str(obj.get("currency") or "usd").lower()
        customer_email = str(obj.get("customer_email") or "")
        payment_intent = str(obj.get("payment_intent") or obj.get("id") or "")
        recurring = True
    if amount_minor <= 0:
        return None
    offer = resolve_offer(obj, amount_minor)
    payment_reference = payment_intent or f"{event.get('id')}-{amount_minor}"
    return {
        "payment_reference": payment_reference,
        "event_id": str(event.get("id") or ""),
        "event_type": event_type,
        "customer_email": str(customer_email or ""),
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
            payment_cursor = connection.execute(
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
            if payment_cursor.rowcount == 0:
                payment = None
    return True, payment


def mark_fulfillment_notified(payment_reference: str) -> bool:
    """Atomically transition a pending payment to FULFILLMENT_NOTIFIED. Returns True if claimed."""
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE payments
            SET status = 'FULFILLMENT_NOTIFIED'
            WHERE payment_reference = ? AND status = 'PAID_PENDING_FULFILLMENT'
            """,
            (payment_reference,),
        )
        return cursor.rowcount == 1


def notify_fulfillment(payment: dict) -> bool:
    if not FULFILLMENT_WEBHOOK_URL:
        return False
    if not FULFILLMENT_WEBHOOK_URL.startswith("https://"):
        log("FULFILLMENT_WEBHOOK_URL rejected: HTTPS is required")
        return False
    # Claim first so concurrent/redelivered events cannot double-notify.
    if not mark_fulfillment_notified(payment["payment_reference"]):
        log(f"Fulfillment already claimed or not pending: {payment['payment_reference']}")
        return True  # Treat as success; already handled.
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
        "idempotency_key": payment["payment_reference"],
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "garcar-payments/2.0",
        "Idempotency-Key": payment["payment_reference"],
    }
    if FULFILLMENT_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {FULFILLMENT_WEBHOOK_TOKEN}"
    try:
        with urlopen(Request(FULFILLMENT_WEBHOOK_URL, data=body, headers=headers, method="POST"), timeout=10) as response:
            ok = 200 <= response.status < 300
            if not ok:
                # Revert claim so Stripe redelivery can retry.
                with _connect() as connection:
                    connection.execute(
                        "UPDATE payments SET status = 'PAID_PENDING_FULFILLMENT' WHERE payment_reference = ?",
                        (payment["payment_reference"],),
                    )
            return ok
    except Exception as exc:
        log(f"Fulfillment notification failed: {type(exc).__name__}")
        with _connect() as connection:
            connection.execute(
                "UPDATE payments SET status = 'PAID_PENDING_FULFILLMENT' WHERE payment_reference = ?",
                (payment["payment_reference"],),
            )
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
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {
                "ok": True,
                "webhook_verification": bool(STRIPE_WEBHOOK_SECRET) or ALLOW_UNVERIFIED_WEBHOOKS,
                "fulfillment_configured": bool(FULFILLMENT_WEBHOOK_URL),
                "database": str(DB_PATH),
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
            # Duplicate event: still attempt fulfillment if a pending payment exists for this event
            # (covers prior failed notification that returned 200).
            pending = None
            if payment is None:
                try:
                    event_id = str(event.get("id") or "").strip()
                    with _connect() as connection:
                        row = connection.execute(
                            "SELECT * FROM payments WHERE event_id = ? AND status = ? LIMIT 1",
                            (event_id, "PAID_PENDING_FULFILLMENT"),
                        ).fetchone()
                        if row:
                            pending = dict(row)
                except sqlite3.Error as db_exc:
                    log(f"Database lookup failed on duplicate event: {db_exc}")
                    self._json(500, {"error": "database unavailable", "duplicate": True})
                    return
            if pending and FULFILLMENT_WEBHOOK_URL:
                notified = notify_fulfillment(pending)
                if not notified:
                    self._json(502, {"received": True, "duplicate": True, "fulfillment_retry_failed": True})
                    return
                self._json(200, {"received": True, "duplicate": True, "fulfillment_notified": True})
                return
            self._json(200, {"received": True, "duplicate": True})
            return
        notified = notify_fulfillment(payment) if payment else False
        if payment:
            log(f"PAYMENT QUEUED reference={payment['payment_reference']} sku={payment['sku']} amount_minor={payment['amount_minor']}")
            if FULFILLMENT_WEBHOOK_URL and not notified:
                # Return non-2xx so Stripe retries; payment row already persisted.
                self._json(502, {
                    "received": True,
                    "payment_queued": True,
                    "fulfillment_notified": False,
                    "error": "fulfillment notification failed — will retry on Stripe redelivery",
                })
                return
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
