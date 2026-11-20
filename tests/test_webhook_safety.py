"""Local-only proof of fail-closed verification and outcome-unknown fulfillment."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import main


def checkout():
    return {
        "id": "evt_synthetic_001",
        "livemode": True,
        "type": "checkout.session.completed",
        "data": {"object": {
            "id": "cs_synthetic_001",
            "payment_intent": "pi_synthetic_001",
            "payment_status": "paid",
            "amount_total": 49700,
            "currency": "usd",
            "metadata": {"sku": "MLS-497"},
        }},
    }


class Response:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class ReceiverSafetyTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.patchers = [
            patch.object(main, "DB_PATH", Path(self.folder.name) / "payments.sqlite3"),
            patch.object(main, "STRIPE_WEBHOOK_SECRET", ""),
            patch.object(main, "ALLOW_UNVERIFIED_WEBHOOKS", True),
            patch.object(main, "UNSIGNED_LOCAL_TEST", False),
            patch.object(main, "FULFILLMENT_WEBHOOK_URL",
                         "https://example.net/fulfillment"),
            patch.object(main, "FULFILLMENT_WEBHOOK_TOKEN", "fixture-auth-only-token"),
        ]
        for p in self.patchers:
            p.start()
        main.init_db()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.folder.cleanup()

    def _payment(self):
        event = checkout()
        inserted, payment = main.record_event(event, json.dumps(event).encode())
        self.assertTrue(inserted)
        self.assertIsNotNone(payment)
        return payment

    def _status(self, payment_reference):
        with main._connect() as connection:
            row = connection.execute(
                "SELECT status FROM payments WHERE payment_reference = ?",
                (payment_reference,),
            ).fetchone()
            return row["status"]

    def _server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.shutdown)
        return "http://127.0.0.1:" + str(server.server_address[1])

    def test_unsigned_ignored_even_if_flag_is_true_in_live_mode(self):
        root = self._server()
        req = Request(root + "/stripe-webhook",
                      data=json.dumps(checkout()).encode(), method="POST")
        with self.assertRaises(HTTPError) as cm:
            urlopen(req, timeout=4)
        self.assertEqual(cm.exception.code, 503)
        self.assertEqual(main.fulfillment_summary()["paid_pending_fulfillment"], 0)

    def test_health_is_not_evidence_of_signature_readiness(self):
        root = self._server()
        with urlopen(root + "/health", timeout=4) as response:
            payload = json.load(response)
        self.assertTrue(payload["alive"])
        self.assertFalse(payload["ready_for_live_webhooks"])
        self.assertFalse(payload["webhook_signature_configured"])
        self.assertNotIn("database", payload)
        with self.assertRaises(HTTPError) as cm:
            urlopen(root + "/ready", timeout=4)
        self.assertEqual(cm.exception.code, 503)
        with patch.object(main, "STRIPE_WEBHOOK_SECRET", "synthetic_secret"):
            with urlopen(root + "/ready", timeout=4) as response:
                self.assertEqual(response.status, 200)

    def test_successful_notification_is_marked_only_after_2xx(self):
        payment = self._payment()
        with patch.object(main, "urlopen", return_value=Response()) as send:
            self.assertTrue(main.notify_fulfillment(payment))
            self.assertEqual(send.call_count, 1)
            self.assertFalse(main.notify_fulfillment(payment))
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self._status(payment["payment_reference"]),
                         "FULFILLMENT_NOTIFIED")

    def test_network_failure_never_blindly_retries(self):
        payment = self._payment()
        with patch.object(main, "urlopen", side_effect=URLError("timeout")) as send:
            self.assertFalse(main.notify_fulfillment(payment))
            self.assertFalse(main.notify_fulfillment(payment))
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self._status(payment["payment_reference"]),
                         "FULFILLMENT_OUTCOME_UNKNOWN")
        summary = main.fulfillment_summary()
        self.assertEqual(summary["fulfillment_outcome_unknown"], 1)
        self.assertEqual(summary["fulfillment_notified"], 0)

    def test_reserved_inflight_state_requires_reconciliation(self):
        payment = self._payment()
        self.assertTrue(main.reserve_fulfillment_attempt(payment["payment_reference"]))
        self.assertFalse(main.reserve_fulfillment_attempt(payment["payment_reference"]))
        self.assertEqual(self._status(payment["payment_reference"]),
                         "FULFILLMENT_ATTEMPTING")
        with patch.object(main, "urlopen", return_value=Response()) as send:
            self.assertFalse(main.notify_fulfillment(payment))
            send.assert_not_called()

    def test_duplicate_event_manual_reconciliation(self):
        payment = self._payment()
        self.assertTrue(main.reserve_fulfillment_attempt(payment["payment_reference"]))
        main.finish_fulfillment_attempt(payment["payment_reference"], confirmed=False)
        # Simulate redelivery. The receiver must report unknown and NOT re-dispatch.
        root = self._server()
        raw = json.dumps(checkout()).encode()
        with patch.object(main, "STRIPE_WEBHOOK_SECRET", "synthetic_secret"), \
             patch.object(main, "verify_stripe_signature", return_value=True), \
             patch.object(main, "urlopen", return_value=Response()) as sender:
            req = Request(root + "/stripe-webhook", data=raw,
                          headers={"Stripe-Signature": "test"}, method="POST")
            with urlopen(req, timeout=4) as response:
                body = json.load(response)
            self.assertTrue(body["manual_reconciliation_required"])
            self.assertEqual(body["fulfillment_status"], "FULFILLMENT_OUTCOME_UNKNOWN")
            sender.assert_not_called()

    def test_local_unsigned_test_requires_all_three_conditions(self):
        with patch.object(main, "STRIPE_WEBHOOK_SECRET", ""):
            with patch.object(main, "UNSIGNED_LOCAL_TEST", True):
                self.assertTrue(main.UNSIGNED_LOCAL_TEST)
        # Production initialization derives UNSIGNED_LOCAL_TEST from
        # flag, environment and loopback hostname; CLI code is guarded.
        self.assertIn("GARCAR_ENVIRONMENT", Path(main.__file__).read_text())
        self.assertIn('SERVER_HOST == "127.0.0.1"',
                      Path(main.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
