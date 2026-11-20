"""Loopback receiver for Stripe sandbox deliveries. Run as a separate process."""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path

import main


SANDBOX_DATABASE_ID = 0x47534258


class SandboxHandler(main.Handler):
    expected_livemode = False
    wrong_mode_reason = "non_test_event"

    def do_GET(self):
        if self.path == "/ready":
            self._json(200, {"ready": True, "mode": "sandbox"})
        else:
            self._json(404, {"error": "not found"})


def sandbox_database(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "stripe-sandbox.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("BEGIN IMMEDIATE")
        database_id = db.execute("PRAGMA application_id").fetchone()[0]
        if database_id != SANDBOX_DATABASE_ID:
            if database_id or db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                raise ValueError("Choose an empty directory or an existing Garcar sandbox ledger")
            db.execute(f"PRAGMA application_id = {SANDBOX_DATABASE_ID}")
        db.commit()
    return path


def verify_receipt(directory: Path, session: dict) -> dict:
    if not isinstance(session, dict):
        raise ValueError("Expected a Stripe Checkout Session object")
    session_id = session.get("id")
    intent = session.get("payment_intent")
    metadata = session.get("metadata")
    if (session.get("object") != "checkout.session" or session.get("livemode") is not False
            or not isinstance(session_id, str) or not session_id.startswith("cs_test_")
            or session.get("mode") != "payment" or session.get("status") != "complete"
            or session.get("payment_status") != "paid" or not isinstance(intent, str) or not intent
            or not isinstance(metadata, dict) or metadata.get("sku") != "MLS-497"
            or session.get("amount_total") != 49700 or session.get("currency") != "usd"):
        raise ValueError("Expected a completed, paid USD 497.00 MLS-497 sandbox Checkout Session")

    path = (directory / "stripe-sandbox.sqlite3").resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != SANDBOX_DATABASE_ID:
            raise ValueError("Expected a Garcar sandbox ledger")
        rows = db.execute("""
            SELECT p.payment_reference, p.sku, p.amount_minor, p.currency,
                   e.event_id, e.payload_sha256
            FROM payments p JOIN stripe_events e ON e.event_id = p.event_id
            WHERE p.session_id = ?
        """, (session_id,)).fetchall()
    if len(rows) != 1 or rows[0][:4] != (intent, "MLS-497", 49700, "usd"):
        raise ValueError("Expected exactly one matching receipt for the supplied session")
    return {"matching_receipts": 1, "session_id": session_id, "payment_intent": intent,
            "sku": "MLS-497", "amount_minor": 49700, "currency": "usd",
            "event_id": rows[0][4], "payload_sha256": rows[0][5]}


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=4242)
    parser.add_argument("--verify-session", action="store_true",
                        help="compare Stripe Checkout Session JSON from stdin to the sandbox ledger")
    args = parser.parse_args()
    if args.verify_session:
        try:
            receipt = verify_receipt(args.data_dir, json.load(sys.stdin))
        except (sqlite3.Error, ValueError) as error:
            parser.exit(1, f"Receipt verification failed: {error}\n")
        print(json.dumps(receipt))
        return
    secret = os.environ.get("STRIPE_SANDBOX_WEBHOOK_SECRET", "").strip()
    if not secret:
        parser.error("STRIPE_SANDBOX_WEBHOOK_SECRET is required")

    # These settings apply only to this separate process.
    main.STRIPE_WEBHOOK_SECRET = secret
    main.UNSIGNED_LOCAL_TEST = False
    main.FULFILLMENT_WEBHOOK_URL = ""
    main.FULFILLMENT_WEBHOOK_TOKEN = ""
    os.umask(0o077)
    main.DB_PATH = sandbox_database(args.data_dir)
    main.init_db()
    with ThreadingHTTPServer(("127.0.0.1", args.port), SandboxHandler) as server:
        print(f"Sandbox listening on http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    run()
