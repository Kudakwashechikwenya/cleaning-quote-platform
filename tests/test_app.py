import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import app, get_business, get_default_business_id, get_quote_request, get_quote_requests, init_db


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
        response = self.client.get(f"/b/{business_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Request a quote", response.data)

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
