import hashlib
import io
import os
from unittest.mock import patch
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


    def test_legacy_unsigned_bypass_never_accepts_live_money_event(self):
        event = self.checkout_event("evt_unsigned")
        event["livemode"] = True
        payload = json.dumps(event).encode()
        handler = object.__new__(main.Handler)
        handler.path = "/stripe-webhook"
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)
        responses = []
        handler._json = lambda status, body: responses.append((status, body))
        with patch.object(main, "STRIPE_WEBHOOK_SECRET", ""), patch.dict(
            os.environ, {"ALLOW_UNVERIFIED_WEBHOOKS": "true", "GARCAR_ENV": "development"}
        ):
            handler.do_POST()
        self.assertEqual(responses[0][0], 503)
        self.assertEqual(main.aggregate_revenue()["paid_events"], 0)

    def test_invalid_webhook_signature_cannot_record_a_payment(self):
        event = self.checkout_event("evt_wrong_sig")
        event["livemode"] = True
        payload = json.dumps(event).encode()
        handler = object.__new__(main.Handler)
        handler.path = "/stripe-webhook"
        handler.headers = {"Content-Length": str(len(payload)), "Stripe-Signature": "wrong"}
        handler.rfile = io.BytesIO(payload)
        responses = []
        handler._json = lambda status, body: responses.append((status, body))
        with patch.object(main, "STRIPE_WEBHOOK_SECRET", "whsec_test"):
            handler.do_POST()
        self.assertEqual(responses[0][0], 400)
        self.assertEqual(main.aggregate_revenue()["paid_events"], 0)

    def test_fulfillment_notification_requires_authentication_token(self):
        event = self.checkout_event("evt_no_callback_auth")
        event["data"]["object"]["metadata"] = {"sku": "GAR-REVIEW-497"}
        event["data"]["object"]["payment_intent"] = "pi_no_callback_auth"
        _, payment = main.record_event(event, json.dumps(event).encode())
        with patch.object(main, "FULFILLMENT_WEBHOOK_URL", "https://example.invalid/notify"), patch.object(
            main, "FULFILLMENT_WEBHOOK_TOKEN", ""
        ), patch.object(main, "urlopen", side_effect=AssertionError("MUST NOT send")):
            self.assertFalse(main.notify_fulfillment(payment))
        with main._connect() as db:
            status = db.execute(
                "SELECT status FROM payments WHERE payment_reference = ?",
                ("pi_no_callback_auth",),
            ).fetchone()["status"]
        self.assertEqual(status, "PAID_PENDING_FULFILLMENT")

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
        self.assertEqual(payment["status"], "PAID_MANUAL_REVIEW")

    def test_ambiguous_497_requires_manual_review(self):
        event = self.checkout_event("evt_497")
        event["data"]["object"]["payment_intent"] = "pi_497"
        event["data"]["object"]["amount_total"] = 49700
        payment = main.extract_payment(event)
        self.assertEqual(payment["sku"], "manual-review-497")
        self.assertEqual(payment["status"], "PAID_MANUAL_REVIEW")

    def test_metadata_disambiguates_offer(self):
        event = self.checkout_event("evt_meta")
        obj = event["data"]["object"]
        obj["payment_intent"] = "pi_meta"
        obj["amount_total"] = 49700
        obj["metadata"] = {"sku": "workflow-sprint-497", "offer_name": "$497/wk Sprint", "fulfill_hours": "24"}
        payment = main.extract_payment(event)
        self.assertEqual(payment["sku"], "workflow-sprint-497")
        self.assertEqual(payment["fulfill_hours"], 24)

    def test_unattributed_payment_never_dispatches(self):
        from unittest.mock import patch

        event = self.checkout_event("evt_attribution_hold")
        event["data"]["object"]["payment_intent"] = "pi_attribution_hold"
        event["data"]["object"]["amount_total"] = 49700
        inserted, payment = main.record_event(event, json.dumps(event).encode())
        self.assertTrue(inserted)
        self.assertEqual(payment["status"], "PAID_MANUAL_REVIEW")
        with patch.object(main, "FULFILLMENT_WEBHOOK_URL", "https://example.invalid/notify"):
            with patch.object(main, "urlopen", side_effect=AssertionError("MUST NOT send")):
                self.assertFalse(main.notify_fulfillment(payment))
        with main._connect() as connection:
            row = connection.execute(
                "SELECT status FROM payments WHERE payment_reference = ?",
                ("pi_attribution_hold",),
            ).fetchone()
        self.assertEqual(row["status"], "PAID_MANUAL_REVIEW")

    def test_known_sku_can_be_fulfilled(self):
        event = self.checkout_event("evt_known")
        event["data"]["object"]["metadata"] = {
            "sku": "GAR-REVIEW-497",
            "offer_name": "Lead Follow-Up Review",
        }
        payment = main.extract_payment(event)
        self.assertEqual(payment["status"], "PAID_PENDING_FULFILLMENT")
        self.assertFalse(main.requires_manual_review(payment))

    def test_name_only_metadata_requires_manual_review(self):
        event = self.checkout_event("evt_name_only")
        event["data"]["object"]["metadata"] = {"offer_name": "Unknown"}
        payment = main.extract_payment(event)
        self.assertEqual(payment["status"], "PAID_MANUAL_REVIEW")

    def test_notification_lease_and_stale_claim_protection(self):
        event = self.checkout_event("evt_claim")
        event["data"]["object"]["metadata"] = {"sku": "GAR-REVIEW-497"}
        event["data"]["object"]["payment_intent"] = "pi_claim"
        _, payment = main.record_event(event, json.dumps(event).encode())
        first = main.claim_notification(payment["payment_reference"], now=1000)
        self.assertIsNotNone(first)
        self.assertIsNone(main.claim_notification(payment["payment_reference"], now=1001))
        replacement = main.claim_notification(payment["payment_reference"], now=1121)
        self.assertIsNotNone(replacement)
        self.assertNotEqual(first, replacement)
        self.assertFalse(main.complete_notification(payment["payment_reference"], first))
        self.assertFalse(main.release_notification(payment["payment_reference"], first, "stale"))
        self.assertTrue(main.complete_notification(payment["payment_reference"], replacement))
        self.assertTrue(main.notification_accepted(payment["payment_reference"]))
        self.assertIsNone(main.claim_notification(payment["payment_reference"], now=2000))

    def test_failed_notification_releases_for_retry(self):
        event = self.checkout_event("evt_release")
        event["data"]["object"]["metadata"] = {"sku": "GAR-REVIEW-497"}
        event["data"]["object"]["payment_intent"] = "pi_release"
        _, payment = main.record_event(event, json.dumps(event).encode())
        first = main.claim_notification(payment["payment_reference"], now=10)
        self.assertTrue(main.release_notification(payment["payment_reference"], first, "HTTP_503"))
        second = main.claim_notification(payment["payment_reference"], now=11)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_manual_review_cannot_be_leased(self):
        event = self.checkout_event("evt_manual_hold")
        event["data"]["object"]["payment_intent"] = "pi_manual_hold"
        event["data"]["object"]["amount_total"] = 49700
        _, payment = main.record_event(event, json.dumps(event).encode())
        self.assertEqual(payment["status"], "PAID_MANUAL_REVIEW")
        self.assertIsNone(main.claim_notification(payment["payment_reference"], now=100))

    def test_legacy_unconfirmed_notification_requires_reconciliation(self):
        event = self.checkout_event("evt_old")
        event["data"]["object"]["metadata"] = {"sku": "GAR-REVIEW-497"}
        event["data"]["object"]["payment_intent"] = "pi_old"
        _, payment = main.record_event(event, json.dumps(event).encode())
        with main._connect() as db:
            db.execute(
                "UPDATE payments SET status = 'FULFILLMENT_NOTIFIED' WHERE payment_reference = ?",
                (payment["payment_reference"],),
            )
        main.init_db()
        with main._connect() as db:
            status = db.execute(
                "SELECT status FROM payments WHERE payment_reference = ?",
                (payment["payment_reference"],),
            ).fetchone()["status"]
        self.assertEqual(status, "DELIVERY_RECONCILIATION_REQUIRED")
        self.assertIsNone(main.claim_notification(payment["payment_reference"], now=100))

    def test_claim_retry_budget_exhaustion(self):
        event = self.checkout_event("evt_exhaust")
        event["data"]["object"]["metadata"] = {"sku": "GAR-REVIEW-497"}
        event["data"]["object"]["payment_intent"] = "pi_exhaust"
        _, payment = main.record_event(event, json.dumps(event).encode())
        reference = payment["payment_reference"]
        for i in range(main.MAX_NOTIFICATION_ATTEMPTS):
            token = main.claim_notification(reference, now=100 + i)
            self.assertTrue(token)
            self.assertTrue(main.release_notification(reference, token, "HTTP_502"))
        self.assertIsNone(main.claim_notification(reference, now=10000))
        with main._connect() as db:
            state = db.execute(
                "SELECT status FROM payments WHERE payment_reference = ?", (reference,)
            ).fetchone()["status"]
        self.assertEqual(state, "DELIVERY_MANUAL_INTERVENTION")

    def test_unpaid_checkout_is_not_queued(self):
        event = self.checkout_event("evt_unpaid")
        event["data"]["object"]["payment_status"] = "unpaid"
        self.assertIsNone(main.extract_payment(event))

    def test_metrics_authorization_fails_closed(self):
        previous = main.METRICS_READ_TOKEN
        try:
            main.METRICS_READ_TOKEN = ""
            self.assertFalse(main.metrics_authorized("Bearer any"))
            main.METRICS_READ_TOKEN = "private-token-value"
            self.assertFalse(main.metrics_authorized(""))
            self.assertFalse(main.metrics_authorized("Bearer wrong"))
            self.assertTrue(main.metrics_authorized("Bearer private-token-value"))
        finally:
            main.METRICS_READ_TOKEN = previous

    def test_currency_totals_are_never_mixed(self):
        usd = self.checkout_event("evt_usd")
        payload = json.dumps(usd).encode()
        main.record_event(usd, payload)
        eur = self.checkout_event("evt_eur")
        eur["data"]["object"]["payment_intent"] = "pi_eur"
        eur["data"]["object"]["currency"] = "eur"
        main.record_event(eur, json.dumps(eur).encode())
        totals = main.aggregate_revenue()
        self.assertEqual(totals["paid_events"], 2)
        self.assertIsNone(totals["gross_collected_minor"])
        self.assertEqual(totals["by_currency"]["usd"]["gross_collected_minor"], 4700)
        self.assertEqual(totals["by_currency"]["eur"]["gross_collected_minor"], 4700)


if __name__ == "__main__":
    unittest.main()
