"""Exercise a signed local fixture through the sandbox HTTP listener and SQLite."""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import sandbox_receiver


SCRIPT = Path(sandbox_receiver.__file__).resolve()


@contextmanager
def listener(directory, secret):
    env = {**os.environ, "STRIPE_SANDBOX_WEBHOOK_SECRET": secret}
    with subprocess.Popen(
        [sys.executable, str(SCRIPT), "--data-dir", str(directory), "--port", "0"],
        env=env, stdout=subprocess.PIPE, text=True,
    ) as process:
        try:
            prefix = "Sandbox listening on "
            line = process.stdout.readline().strip()
            if not line.startswith(prefix):
                raise RuntimeError(f"Sandbox failed to start: {line}")
            yield line[len(prefix):]
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def deliver(root, event, secret):
    body = json.dumps(event, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    request = Request(
        root + "/stripe-webhook", data=body,
        headers={"Content-Type": "application/json",
                 "Stripe-Signature": f"t={timestamp},v1={signature.hexdigest()}"},
    )
    with urlopen(request, timeout=5) as response:
        return response.status, json.load(response)


class SandboxCheckoutTests(unittest.TestCase):
    def test_production_handler_ignores_signed_test_events(self):
        secret = secrets.token_hex(32)
        app = sandbox_receiver.main
        with patch.object(app, "STRIPE_WEBHOOK_SECRET", secret), patch.object(app, "record_event") as record:
            with ThreadingHTTPServer(("127.0.0.1", 0), app.Handler) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    response = deliver(f"http://127.0.0.1:{server.server_port}", {"livemode": False}, secret)
                finally:
                    server.shutdown()
                    thread.join()
        self.assertEqual(response, (200, {"received": True, "ignored": True, "reason": "non_live_event"}))
        record.assert_not_called()

    def test_one_receipt_after_restart_and_redelivery(self):
        secret = secrets.token_hex(32)
        event = {
            "id": "fixture_event_mls497", "type": "checkout.session.completed", "livemode": False,
            "data": {"object": {
                "object": "checkout.session", "id": "cs_test_local_fixture_mls497", "livemode": False,
                "status": "complete", "mode": "payment",
                "payment_intent": "fixture_intent_mls497", "payment_status": "paid",
                "amount_total": 49700, "currency": "usd", "metadata": {"sku": "MLS-497"},
            }},
        }
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            database = directory / "stripe-sandbox.sqlite3"
            with listener(directory, secret) as root:
                self.assertEqual(deliver(root, event, secret), (200, {
                    "received": True, "payment_queued": True, "fulfillment_notified": False,
                }))

            with listener(directory, secret) as root:
                status, replay = deliver(root, event, secret)
                self.assertEqual(status, 200)
                self.assertIs(replay["duplicate"], True)
                live = {**event, "id": "fixture_live_event", "livemode": True,
                        "data": {"object": {**event["data"]["object"], "livemode": True}}}
                self.assertEqual(deliver(root, live, secret), (200, {
                    "received": True, "ignored": True, "reason": "non_test_event",
                }))

            with closing(sqlite3.connect(database)) as db:
                receipts = db.execute("""
                    SELECT session_id, payment_reference, sku, amount_minor, currency, status
                    FROM payments
                """).fetchall()
                self.assertEqual(receipts, [("cs_test_local_fixture_mls497", "fixture_intent_mls497",
                                            "MLS-497", 49700, "usd", "PAID_PENDING_FULFILLMENT")])
                events = db.execute("SELECT event_id, payload_sha256 FROM stripe_events").fetchall()
                raw = json.dumps(event, separators=(",", ":")).encode()
                self.assertEqual(events, [(event["id"], hashlib.sha256(raw).hexdigest())])
            session = event["data"]["object"]
            proof = subprocess.run(
                [sys.executable, str(SCRIPT), "--data-dir", str(directory), "--verify-session"],
                input=json.dumps(session), capture_output=True, text=True, check=True,
            )
            self.assertEqual(json.loads(proof.stdout)["event_id"], event["id"])
            with self.assertRaisesRegex(ValueError, "exactly one"):
                sandbox_receiver.verify_receipt(directory, {**session, "payment_intent": "other"})
            with self.assertRaisesRegex(ValueError, "sandbox Checkout Session"):
                sandbox_receiver.verify_receipt(directory, {**session, "livemode": True})

    def test_sandbox_requires_its_own_signing_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "STRIPE_SANDBOX_WEBHOOK_SECRET": "",
                   "STRIPE_WEBHOOK_SECRET": secrets.token_hex(32), "ALLOW_UNVERIFIED_WEBHOOKS": "true"}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--data-dir", folder],
                env=env, text=True, capture_output=True, timeout=5,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("STRIPE_SANDBOX_WEBHOOK_SECRET is required", result.stderr)
            self.assertFalse((Path(folder) / "stripe-sandbox.sqlite3").exists())

    def test_existing_unmarked_ledger_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with closing(sqlite3.connect(directory / "stripe-sandbox.sqlite3")) as db:
                db.execute("CREATE TABLE payments (payment_reference TEXT PRIMARY KEY)")
            with self.assertRaisesRegex(ValueError, "sandbox ledger"):
                sandbox_receiver.sandbox_database(directory)
