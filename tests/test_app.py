import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import app, get_business, get_business_by_email, get_default_business_id, get_quote_request, get_quote_requests, init_db


class QuotePlatformTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        app.config["DEFAULT_BUSINESS_ID"] = None
        self.database_directory = TemporaryDirectory()
        app.config["DATABASE_PATH"] = str(Path(self.database_directory.name) / "test.db")
        init_db()
        self.client = app.test_client()
        with self.client.session_transaction() as active_session:
            active_session["business_id"] = get_default_business_id()

    def tearDown(self):
        self.database_directory.cleanup()

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "ok")

    def test_dashboard_requires_login(self):
        with self.client.session_transaction() as active_session:
            active_session.clear()
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_settings_requires_login(self):
        with self.client.session_transaction() as active_session:
            active_session.clear()
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

    def test_business_can_register_and_login(self):
        with self.client.session_transaction() as active_session:
            active_session.clear()
        response = self.client.post("/register", data={"name": "Bright Homes", "email": "owner@bright.test", "password": "secure1234"})
        self.assertEqual(response.status_code, 302)
        self.client.post("/logout")
        response = self.client.post("/login", data={"email": "owner@bright.test", "password": "secure1234"})
        self.assertEqual(response.status_code, 302)

    def test_business_has_public_quote_page(self):
        business_id = get_default_business_id()
        business = get_business(business_id)
        response = self.client.get(f"/q/{business.slug}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Request a quote", response.data)

    def test_root_is_platform_homepage_not_a_tenant_quote_page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Built for cleaning businesses", response.data)
        self.assertNotIn(b'name="business_id"', response.data)

    def test_registration_creates_unique_tenant_quote_page(self):
        with self.client.session_transaction() as active_session:
            active_session.clear()
        response = self.client.post("/register", data={"name": "Sparkle Cleaning", "email": "sparkle@example.com", "password": "secure1234"})
        self.assertEqual(response.status_code, 302)
        business = get_business_by_email("sparkle@example.com")
        self.assertEqual(business.slug, "sparkle-cleaning")
        quote_page = self.client.get(f"/q/{business.slug}")
        self.assertEqual(quote_page.status_code, 200)
        self.assertIn(b"Sparkle Cleaning", quote_page.data)

    def test_customer_request_creates_estimate(self):
        business_id = get_default_business_id()
        response = self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Alex", "email": "alex@example.com", "phone": "555-0100", "property_type": "home", "bedrooms": "3", "bathrooms": "2", "frequency": "weekly", "notes": "Kitchen priority"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("/request-received/1", response.location)
        quote_request = get_quote_requests()[0]
        self.assertEqual(quote_request.estimated_price, 119)
        self.assertTrue(quote_request.confirmation_token)
        self.assertEqual(self.client.get(f"/request-received/{quote_request.id}").status_code, 404)
        self.assertEqual(self.client.get(response.location).status_code, 200)

    def test_new_request_notifies_owner_and_customer(self):
        business_id = get_default_business_id()
        with patch("app.send_email", return_value=True) as mocked_send:
            response = self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Taylor", "email": "taylor@example.com", "phone": "555-0110", "property_type": "home", "bedrooms": "2", "bathrooms": "1", "frequency": "monthly"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(mocked_send.call_count, 2)
        recipients = [call.args[0] for call in mocked_send.call_args_list]
        self.assertIn("demo@clean.co", recipients)
        self.assertIn("taylor@example.com", recipients)

    def test_proposal_generation_updates_request(self):
        business_id = get_default_business_id()
        self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Alex", "email": "alex@example.com", "phone": "555-0100", "property_type": "home", "bedrooms": "2", "bathrooms": "1", "frequency": "one-off"})
        quote_request = get_quote_requests()[0]
        response = self.client.post(f"/api/quote-requests/{quote_request.id}/proposal")
        self.assertEqual(response.status_code, 200)
        updated_request = get_quote_request(quote_request.id)
        self.assertEqual(updated_request.status, "quoted")
        self.assertIn("Alex", updated_request.proposal)
        self.assertTrue(updated_request.proposal.endswith("Clean Co."))

    def test_owner_can_edit_and_save_proposal(self):
        business_id = get_default_business_id()
        self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Alex", "email": "alex@example.com", "phone": "555-0100", "property_type": "home", "bedrooms": "2", "bathrooms": "1", "frequency": "one-off"})
        quote_request = get_quote_requests()[0]
        response = self.client.post(f"/quote-requests/{quote_request.id}/proposal/save", data={"proposal": "A custom proposal draft."})
        self.assertEqual(response.status_code, 302)
        updated_request = get_quote_request(quote_request.id)
        self.assertEqual(updated_request.proposal, "A custom proposal draft.")
        self.assertEqual(updated_request.status, "quoted")

    def test_owner_can_email_proposal_and_mark_it_sent(self):
        business_id = get_default_business_id()
        self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Alex", "email": "alex@example.com", "phone": "555-0100", "property_type": "home", "bedrooms": "2", "bathrooms": "1", "frequency": "one-off"})
        quote_request = get_quote_requests()[0]
        with patch("app.send_email", return_value=True) as mocked_send:
            response = self.client.post(f"/quote-requests/{quote_request.id}/proposal/send", data={"proposal": "Your final proposal."})
        self.assertEqual(response.status_code, 302)
        mocked_send.assert_called_once()
        self.assertEqual(mocked_send.call_args.args[0], "alex@example.com")
        updated_request = get_quote_request(quote_request.id)
        self.assertEqual(updated_request.status, "sent")
        self.assertEqual(updated_request.proposal, "Your final proposal.")

    def test_failed_email_does_not_mark_proposal_sent(self):
        business_id = get_default_business_id()
        self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Alex", "email": "alex@example.com", "phone": "555-0100", "property_type": "home", "bedrooms": "2", "bathrooms": "1", "frequency": "one-off"})
        quote_request = get_quote_requests()[0]
        with patch("app.send_email", return_value=False):
            response = self.client.post(f"/quote-requests/{quote_request.id}/proposal/send", data={"proposal": "Your final proposal."})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_quote_request(quote_request.id).status, "new")

    def test_customer_can_accept_sent_proposal_using_private_token(self):
        business_id = get_default_business_id()
        self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Alex", "email": "alex@example.com", "phone": "555-0100", "property_type": "home", "bedrooms": "2", "bathrooms": "1", "frequency": "one-off"})
        quote_request = get_quote_requests()[0]
        with patch("app.send_email", return_value=True):
            self.client.post(f"/quote-requests/{quote_request.id}/proposal/send", data={"proposal": "Your final proposal."})
        response = self.client.post(f"/request-received/{quote_request.confirmation_token}/accept")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_quote_request(quote_request.id).status, "accepted")
        self.assertEqual(self.client.post(f"/request-received/{quote_request.confirmation_token}/decline").status_code, 404)

    def test_owner_can_update_business_identity_and_password(self):
        business_id = get_default_business_id()
        response = self.client.post("/settings", data={
            "name": "Fresh Start Cleaning",
            "email": "owner@freshstart.test",
            "current_password": "demo-password",
            "new_password": "new-secure-password",
        })
        self.assertEqual(response.status_code, 302)
        updated_business = get_business(business_id)
        self.assertEqual(updated_business.name, "Fresh Start Cleaning")
        self.assertEqual(updated_business.email, "owner@freshstart.test")

        self.client.post("/logout")
        old_login = self.client.post("/login", data={"email": "demo@clean.co", "password": "demo-password"})
        self.assertEqual(old_login.status_code, 401)
        new_login = self.client.post("/login", data={"email": "owner@freshstart.test", "password": "new-secure-password"})
        self.assertEqual(new_login.status_code, 302)

    def test_settings_rejects_incorrect_current_password(self):
        business_id = get_default_business_id()
        response = self.client.post("/settings", data={
            "name": "Wrong Update",
            "email": "wrong@example.com",
            "current_password": "incorrect",
            "new_password": "",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(get_business(business_id).name, "Clean Co.")

    def test_quote_request_survives_new_database_connection(self):
        business_id = get_default_business_id()
        self.client.post("/quote-requests", data={"business_id": business_id, "customer_name": "Morgan", "email": "morgan@example.com", "phone": "555-0101", "property_type": "apartment", "bedrooms": "1", "bathrooms": "1", "frequency": "monthly"})
        stored_request = get_quote_requests()[0]
        reloaded_request = get_quote_request(stored_request.id)
        self.assertEqual(reloaded_request.customer_name, "Morgan")

    def test_invalid_quote_input_returns_400(self):
        response = self.client.post("/quote-requests", data={"business_id": get_default_business_id(), "customer_name": "Alex", "email": "not-an-email", "phone": "555-0100", "property_type": "invalid", "bedrooms": "lots", "bathrooms": "1", "frequency": "weekly"})
        self.assertEqual(response.status_code, 400)

    def test_post_without_csrf_token_is_rejected(self):
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            response = self.client.post("/logout")
            self.assertEqual(response.status_code, 400)
        finally:
            app.config["WTF_CSRF_ENABLED"] = False

    def test_security_headers_are_present(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
