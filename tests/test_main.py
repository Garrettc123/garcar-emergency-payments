import hashlib
import hmac
import json
import tempfile
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
