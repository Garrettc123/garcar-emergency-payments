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
NOTIFICATION_LEASE_SECONDS = 120
MAX_NOTIFICATION_ATTEMPTS = 12
METRICS_READ_TOKEN = os.environ.get("METRICS_READ_TOKEN", "")
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
        # Existing production volumes retain all payment evidence. Add lease
        # columns incrementally; never drop/recreate the payments table.
        existing = {
            row["name"] for row in connection.execute("PRAGMA table_info(payments)")
        }
        additions = {
            "delivery_claim_token": "TEXT",
            "delivery_claim_until": "INTEGER",
            "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
            "delivery_accepted_at": "TEXT",
            "delivery_last_error": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE payments ADD COLUMN {name} {declaration}")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_delivery_state "
            "ON payments(status, delivery_claim_until)"
        )
        # Old versions marked FULFILLMENT_NOTIFIED BEFORE actually sending.
        # They are ambiguous: do not resend or silently treat as delivered.
        connection.execute(
            """UPDATE payments
               SET status = 'DELIVERY_RECONCILIATION_REQUIRED'
               WHERE status = 'FULFILLMENT_NOTIFIED'
                 AND delivery_accepted_at IS NULL"""
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


def metrics_authorized(authorization: str) -> bool:
    """Fail closed when no private metrics token has been provisioned."""
    return bool(METRICS_READ_TOKEN) and hmac.compare_digest(
        authorization, f"Bearer {METRICS_READ_TOKEN}"
    )


def _invoice_is_recurring(obj: dict) -> bool:
    """Recognize both modern and legacy Stripe subscription invoice shapes."""
    parent = obj.get("parent")
    if isinstance(parent, dict) and parent.get("type") == "subscription_details":
        return True
    if obj.get("subscription"):
        return True
    return str(obj.get("billing_reason") or "").startswith("subscription_")


def _metadata(obj: dict) -> dict:
    """Use Stripe's immutable subscription metadata snapshot when present.

    An invoice can have empty top-level metadata even when its subscription
    identifies a product. Conflicting explicit SKUs require manual attribution.
    """
    direct = obj.get("metadata") or {}
    direct = direct if isinstance(direct, dict) else {}
    snapshot = {}
    if _invoice_is_recurring(obj):
        parent = obj.get("parent")
        if isinstance(parent, dict) and parent.get("type") == "subscription_details":
            details = parent.get("subscription_details") or {}
        else:
            details = obj.get("subscription_details") or {}
        if isinstance(details, dict) and isinstance(details.get("metadata"), dict):
            snapshot = details["metadata"]
    direct_sku = str(direct.get("sku") or direct.get("offer_id") or "").strip()
    snapshot_sku = str(snapshot.get("sku") or snapshot.get("offer_id") or "").strip()
    if direct_sku and snapshot_sku and direct_sku != snapshot_sku:
        return {"sku": "manual-review-conflicting-sku"}
    return {**snapshot, **direct}


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
    if amount_minor == 49700:
        return {
            "sku": "manual-review-497",
            "name": "$497 offer (manual review — add sku metadata)",
            "fulfill_hours": 48,
        }
    return {
        "sku": "manual-review-unattributed",
        "name": f"Unattributed payment ({amount_minor} minor units) — manual review",
        "fulfill_hours": 48,
    }


def requires_manual_review(payment: dict) -> bool:
    """An unattributed receipt is not an authorization to fulfill an arbitrary product."""
    sku = str(payment.get("sku") or "").strip()
    return not sku or sku == "metadata-offer" or sku.startswith("manual-review-")


def extract_payment(event: dict) -> dict | None:
    event_type = str(event.get("type") or "")
    if event_type not in FULFILLABLE_EVENTS:
        return None
    obj = (event.get("data") or {}).get("object") or {}
    if not isinstance(obj, dict):
        return None
    if event_type.startswith("checkout.session"):
        # Subscription Checkouts and their first paid invoice describe the
        # same collection. Let invoice.paid be the sole subscription receipt
        # to prevent double-counting and duplicate fulfillment notifications.
        # One-time Checkout sessions continue through the normal paid gate.
        if str(obj.get("mode") or "").lower() == "subscription":
            return None
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
        # Not every paid invoice is a recurring subscription collection.
        recurring = _invoice_is_recurring(obj)
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
        "status": "PAID_MANUAL_REVIEW" if requires_manual_review(offer) else "PAID_PENDING_FULFILLMENT",
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


def claim_notification(payment_reference: str, now: int | None = None) -> str | None:
    """Lease one eligible payment; a crash leaves an expiring recoverable claim.

    The compare-and-swap UPDATE prevents multiple receiver threads from sending
    the same payment concurrently. The downstream endpoint must also dedupe
    Idempotency-Key because failure after an HTTP 2xx may be ambiguous.
    """
    issued_at = int(time.time()) if now is None else now
    token = os.urandom(16).hex()
    with _connect() as connection:
        changed = connection.execute(
            """
            UPDATE payments
               SET status = 'NOTIFICATION_IN_FLIGHT',
                   delivery_claim_token = ?,
                   delivery_claim_until = ?,
                   delivery_attempts = delivery_attempts + 1
             WHERE payment_reference = ?
               AND delivery_attempts < ?
               AND sku <> 'metadata-offer'
               AND sku NOT LIKE 'manual-review-%'
               AND TRIM(sku) <> ''
               AND (
                    status = 'PAID_PENDING_FULFILLMENT'
                    OR (
                        status = 'NOTIFICATION_IN_FLIGHT'
                        AND delivery_claim_until IS NOT NULL
                        AND delivery_claim_until <= ?
                    )
               )
            """,
            (token, issued_at + NOTIFICATION_LEASE_SECONDS,
             payment_reference, MAX_NOTIFICATION_ATTEMPTS, issued_at),
        )
        return token if changed.rowcount == 1 else None


def notification_accepted(payment_reference: str) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT status FROM payments WHERE payment_reference = ?",
            (payment_reference,),
        ).fetchone()
    return bool(row and row["status"] == "NOTIFICATION_ACCEPTED")


def complete_notification(payment_reference: str, token: str) -> bool:
    """Persist explicit downstream HTTP acceptance, not customer fulfillment."""
    with _connect() as connection:
        updated = connection.execute(
            """UPDATE payments
               SET status = 'NOTIFICATION_ACCEPTED',
                   delivery_accepted_at = ?,
                   delivery_claim_token = NULL,
                   delivery_claim_until = NULL,
                   delivery_last_error = NULL
               WHERE payment_reference = ?
                 AND delivery_claim_token = ?
                 AND status = 'NOTIFICATION_IN_FLIGHT'""",
            (utc_now(), payment_reference, token),
        )
        return updated.rowcount == 1


def release_notification(payment_reference: str, token: str, error_kind: str) -> bool:
    """Only the active lease owner may release or exhaust a delivery claim."""
    with _connect() as connection:
        updated = connection.execute(
            """UPDATE payments
               SET status = CASE
                   WHEN delivery_attempts >= ? THEN 'DELIVERY_MANUAL_INTERVENTION'
                   ELSE 'PAID_PENDING_FULFILLMENT'
               END,
               delivery_claim_token = NULL,
               delivery_claim_until = NULL,
               delivery_last_error = ?
               WHERE payment_reference = ?
                 AND delivery_claim_token = ?
                 AND status = 'NOTIFICATION_IN_FLIGHT'""",
            (MAX_NOTIFICATION_ATTEMPTS, error_kind[:100], payment_reference, token),
        )
        return updated.rowcount == 1


def notify_fulfillment(payment: dict) -> bool:
    # This is an internal notification; the receiving service still needs
    # explicit approval before any external outreach, charge or final delivery.
    if requires_manual_review(payment):
        log("Fulfillment held: payment requires manual offer attribution")
        return False
    if not FULFILLMENT_WEBHOOK_URL:
        return False
    if not FULFILLMENT_WEBHOOK_URL.startswith("https://"):
        log("FULFILLMENT_WEBHOOK_URL rejected: HTTPS is required")
        return False
    # Never transmit customer identifiers or payment metadata to an unauthenticated sink.
    if not FULFILLMENT_WEBHOOK_TOKEN:
        log("Fulfillment notification held: authentication token not configured")
        return False
    reference = payment["payment_reference"]
    token = claim_notification(reference)
    if not token:
        # An in-flight (or exhausted) claim is NOT successful delivery.
        return notification_accepted(reference)
    payload = {
        "event": "garcar.payment.received",
        "payment_reference": reference,
        "customer_email": payment["customer_email"],
        "amount_minor": payment["amount_minor"],
        "currency": payment["currency"],
        "offer_name": payment["offer_name"],
        "sku": payment["sku"],
        "fulfill_hours": payment["fulfill_hours"],
        "created_at": payment["created_at"],
        "idempotency_key": reference,
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "garcar-payments/2.1",
        "Idempotency-Key": reference,
    }
    if FULFILLMENT_WEBHOOK_TOKEN:
        headers["Authorization"] = f"Bearer {FULFILLMENT_WEBHOOK_TOKEN}"
    try:
        with urlopen(
            Request(FULFILLMENT_WEBHOOK_URL, data=body, headers=headers, method="POST"),
            timeout=10,
        ) as response:
            if not (200 <= response.status < 300):
                release_notification(reference, token, f"HTTP_{response.status}")
                return False
        if complete_notification(reference, token):
            return True
        log("Delivery acknowledgment race: lease changed; manual reconciliation required")
        return False
    except Exception as exc:
        # Never write response bodies, URLs with query tokens, or credentials.
        log(f"Fulfillment notification failed: {type(exc).__name__}")
        release_notification(reference, token, type(exc).__name__)
        return False


def aggregate_revenue() -> dict:
    # PaymentIntents and the Stripe ledger remain authoritative; this is only
    # a receiver-level record of distinct paid payment references.
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT currency, COUNT(*) AS paid_events,
                   COALESCE(SUM(amount_minor), 0) AS gross_minor,
                   COALESCE(SUM(CASE WHEN recurring = 1 THEN amount_minor ELSE 0 END), 0)
                       AS recurring_collections_minor
            FROM payments
            GROUP BY currency
            ORDER BY currency
            """
        ).fetchall()
    by_currency = {
        row["currency"]: {
            "paid_events": row["paid_events"],
            "gross_collected_minor": row["gross_minor"],
            "recurring_collections_minor": row["recurring_collections_minor"],
        }
        for row in rows
    }
    unambiguous = next(iter(by_currency.values())) if len(by_currency) == 1 else None
    return {
        "paid_events": sum(r["paid_events"] for r in by_currency.values()),
        # Do not add incompatible minor units (USD + EUR, etc.).
        "gross_collected_minor": (
            unambiguous["gross_collected_minor"] if unambiguous else
            (0 if not by_currency else None)
        ),
        "recurring_collections_minor": (
            unambiguous["recurring_collections_minor"] if unambiguous else
            (0 if not by_currency else None)
        ),
        "by_currency": by_currency,
        "currency_note": "Totals are grouped by currency; global minor-unit sums are omitted when currencies differ.",
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
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._json(200, {
                "ok": True,
                "webhook_verification": bool(STRIPE_WEBHOOK_SECRET),
                "fulfillment_configured": bool(FULFILLMENT_WEBHOOK_URL and FULFILLMENT_WEBHOOK_TOKEN),
            })
        elif path == "/readyz":
            try:
                with _connect() as connection:
                    connection.execute("SELECT 1").fetchone()
                database_ready = True
            except sqlite3.Error:
                database_ready = False
            ready = bool(STRIPE_WEBHOOK_SECRET) and database_ready
            self._json(200 if ready else 503, {
                "ready": ready,
                "webhook_verification": bool(STRIPE_WEBHOOK_SECRET),
                "database_ready": database_ready,
                "fulfillment_configured": bool(FULFILLMENT_WEBHOOK_URL and FULFILLMENT_WEBHOOK_TOKEN),
            })
        elif path in ("/revenue", "/mrr"):
            if not METRICS_READ_TOKEN:
                self._json(503, {"error": "private metrics not configured"})
                return
            if not metrics_authorized(self.headers.get("Authorization", "")):
                self._json(401, {"error": "unauthorized"})
                return
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
        if not STRIPE_WEBHOOK_SECRET:
            log("Webhook rejected: STRIPE_WEBHOOK_SECRET is not configured")
            self._json(503, {"error": "webhook verification not configured"})
            return
        if not verify_stripe_signature(payload, signature, STRIPE_WEBHOOK_SECRET):
            self._json(400, {"error": "invalid signature"})
            return
        try:
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("Stripe event must be an object")
            # A production endpoint must never treat synthetic / test-mode events as revenue.
            if event.get("livemode") is not True:
                self._json(200, {"received": True, "ignored": True, "reason": "non_live_event"})
                return
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
                            "SELECT * FROM payments WHERE event_id = ? AND status IN (?, ?) LIMIT 1",
                            (event_id, "PAID_PENDING_FULFILLMENT", "NOTIFICATION_IN_FLIGHT"),
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
        notified = notify_fulfillment(payment) if payment and not requires_manual_review(payment) else False
        if payment:
            if requires_manual_review(payment):
                log(f"PAYMENT HELD for offer attribution reference={payment['payment_reference']}")
                self._json(200, {
                    "received": True,
                    "payment_queued": True,
                    "fulfillment_notified": False,
                    "fulfillment_held": True,
                    "reason": "manual_offer_attribution",
                })
                return
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
    if not STRIPE_WEBHOOK_SECRET:
        log("STRIPE_WEBHOOK_SECRET missing: health checks pass, but webhooks are rejected")
    log(f"Garcar Payments v2 listening on 0.0.0.0:{PORT}; database={DB_PATH}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
