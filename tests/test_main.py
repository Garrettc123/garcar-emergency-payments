import hashlib
import hmac
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import main


class PaymentReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        main.DB_PATH = Path(self.temp_dir.name) / "payments.sqlite3"
        main.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    def checkout_event(self, event_id="evt_1"):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {"object": {
                "id": "cs_1",
                "payment_intent": "pi_1",
                "payment_status": "paid",
                "amount_total": 4700,
                "currency": "usd",
                "customer_details": {"email": "buyer@example.com"},
            }},
        }

    def test_signature_verification(self):
        payload = b'{"id":"evt_1"}'
        timestamp = int(time.time())
        signature = hmac.new(b"whsec_test", f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        header = f"t={timestamp},v1={signature}"
        self.assertTrue(main.verify_stripe_signature(payload, header, "whsec_test", now=timestamp))
        self.assertFalse(main.verify_stripe_signature(payload, header, "wrong", now=timestamp))

    def test_idempotent_event_and_payment(self):
        event = self.checkout_event()
        event["data"]["object"]["metadata"] = {"sku": "contractor-audit-47", "offer_name": "Contractor Audit"}
        payload = json.dumps(event).encode()
        inserted, payment = main.record_event(event, payload)
        duplicate, second_payment = main.record_event(event, payload)
        self.assertTrue(inserted)
        self.assertEqual(payment["sku"], "contractor-audit-47")
        self.assertFalse(duplicate)
        self.assertIsNone(second_payment)
        totals = main.aggregate_revenue()
        self.assertEqual(totals["paid_events"], 1)
        self.assertEqual(totals["gross_collected_minor"], 4700)

    def test_untagged_2500_never_guesses_offer(self):
        event = self.checkout_event("evt_2500")
        event["data"]["object"]["amount_total"] = 250000
        payment = main.extract_payment(event)
        self.assertEqual(payment["sku"], "manual-review-unattributed")

    def test_ambiguous_497_requires_manual_review(self):
        event = self.checkout_event("evt_497")
        event["data"]["object"]["payment_intent"] = "pi_497"
        event["data"]["object"]["amount_total"] = 49700
        payment = main.extract_payment(event)
        self.assertEqual(payment["sku"], "manual-review-497")

    def test_metadata_disambiguates_offer(self):
        event = self.checkout_event("evt_meta")
        obj = event["data"]["object"]
        obj["payment_intent"] = "pi_meta"
        obj["amount_total"] = 49700
        obj["metadata"] = {"sku": "workflow-sprint-497", "offer_name": "$497/wk Sprint", "fulfill_hours": "24"}
        payment = main.extract_payment(event)
        self.assertEqual(payment["sku"], "workflow-sprint-497")
        self.assertEqual(payment["fulfill_hours"], 24)

    def test_unpaid_checkout_is_not_queued(self):
        event = self.checkout_event("evt_unpaid")
        event["data"]["object"]["payment_status"] = "unpaid"
        self.assertIsNone(main.extract_payment(event))

    def test_unattributed_payment_is_held_without_handoff(self):
        event = self.checkout_event("evt_held_497")
        event["data"]["object"]["amount_total"] = 49700
        event["data"]["object"]["payment_intent"] = "pi_held_497"
        payment = main.extract_payment(event)
        self.assertEqual(payment["status"], "PAID_MANUAL_REVIEW")
        with patch.object(main, "FULFILLMENT_WEBHOOK_URL", "https://internal.example/fulfill"):
            with patch.object(main, "urlopen", side_effect=AssertionError("must not call")):
                self.assertFalse(main.notify_fulfillment(payment))
        inserted, _ = main.record_event(event, json.dumps(event).encode())
        self.assertTrue(inserted)
        self.assertEqual(main.fulfillment_summary()["paid_manual_review"], 1)

    def test_name_only_offer_is_held(self):
        event = self.checkout_event("evt_name_only")
        event["data"]["object"]["metadata"] = {"offer_name": "ambiguous"}
        self.assertEqual(main.extract_payment(event)["status"], "PAID_MANUAL_REVIEW")

    def test_private_metrics_bearer_check(self):
        with patch.object(main, "METRICS_READ_TOKEN", "long-read-only-metrics-token"):
            self.assertFalse(main.metrics_authorized(""))
            self.assertFalse(main.metrics_authorized("Bearer incorrect"))
            self.assertFalse(main.metrics_authorized("Basic long-read-only-metrics-token"))
            self.assertTrue(main.metrics_authorized("Bearer long-read-only-metrics-token"))
        with patch.object(main, "METRICS_READ_TOKEN", ""):
            self.assertFalse(main.metrics_authorized("Bearer long-read-only-metrics-token"))

    def test_downstream_handoff_requires_bearer_token(self):
        event = self.checkout_event("evt_withheld_no_token")
        event["data"]["object"]["metadata"] = {
            "sku": "approved-contract-review",
            "offer_name": "Approved Contract Review",
        }
        payment = main.extract_payment(event)
        self.assertEqual(payment["status"], "PAID_PENDING_FULFILLMENT")
        with patch.object(main, "FULFILLMENT_WEBHOOK_URL", "https://internal.example/fulfill"):
            with patch.object(main, "FULFILLMENT_WEBHOOK_TOKEN", ""):
                with patch.object(main, "urlopen", side_effect=AssertionError("must not send")):
                    self.assertFalse(main.notify_fulfillment(payment))
        self.assertEqual(main.fulfillment_summary()["fulfillment_attempting"], 0)

    def test_private_metrics_http_routes_fail_closed(self):
        class Capture:
            def __init__(self, path, auth):
                self.path = path
                self.headers = {"Authorization": auth}
                self.responses = []

            def _json(self, status, body):
                self.responses.append((status, body))

        for path in ("/revenue", "/mrr", "/fulfillment-status"):
            with self.subTest(path=path):
                with patch.object(main, "METRICS_READ_TOKEN", ""):
                    handler = Capture(path, "")
                    main.Handler.do_GET(handler)
                    self.assertEqual(handler.responses[0][0], 503)
                with patch.object(main, "METRICS_READ_TOKEN", "protected-token"):
                    handler = Capture(path, "Bearer wrong")
                    main.Handler.do_GET(handler)
                    self.assertEqual(handler.responses[0][0], 403)
                    handler = Capture(path, "Bearer protected-token")
                    main.Handler.do_GET(handler)
                    self.assertEqual(handler.responses[0][0], 200)

    def test_signed_unattributed_webhook_acknowledges_manual_hold(self):
        from io import BytesIO
        event = self.checkout_event("evt_signed_manual_hold")
        event["livemode"] = True
        event["data"]["object"]["amount_total"] = 49700
        event["data"]["object"]["payment_intent"] = "pi_signed_manual_hold"
        payload = json.dumps(event).encode()
        timestamp = int(time.time())
        signature = hmac.new(
            b"whsec_local_fixture",
            f"{timestamp}.".encode() + payload,
            hashlib.sha256,
        ).hexdigest()

        class Capture:
            path = "/stripe-webhook"
            headers = {
                "Content-Length": str(len(payload)),
                "Stripe-Signature": f"t={timestamp},v1={signature}",
            }
            rfile = BytesIO(payload)

            def __init__(self):
                self.responses = []

            def _json(self, status, body):
                self.responses.append((status, body))

        with patch.object(main, "STRIPE_WEBHOOK_SECRET", "whsec_local_fixture"):
            with patch.object(main, "FULFILLMENT_WEBHOOK_URL", "https://internal.example/fulfill"):
                with patch.object(main, "urlopen", side_effect=AssertionError("must not call")):
                    handler = Capture()
                    main.Handler.do_POST(handler)
        status, body = handler.responses[0]
        self.assertEqual(status, 200)
        self.assertTrue(body["manual_review_required"])
        self.assertFalse(body["fulfillment_notified"])
        self.assertEqual(main.fulfillment_summary()["paid_manual_review"], 1)


if __name__ == "__main__":
    unittest.main()
