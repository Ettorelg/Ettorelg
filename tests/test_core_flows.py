"""Regressioni sulle regole commerciali e sui flussi sensibili, senza servizi esterni reali."""
import ast
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import Mock, patch


SOURCE = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def load_function(name, scope):
    function = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), scope)
    return scope[name]


class LicenseAndTrialTests(unittest.TestCase):
    def test_license_requires_active_status_and_non_expired_date(self):
        active = load_function("license_is_active", {"date": date, "datetime": datetime})
        self.assertTrue(active("attiva", date.today()))
        self.assertFalse(active("sospesa", date.today() + timedelta(days=10)))
        self.assertFalse(active("attiva", date.today() - timedelta(days=1)))

    def test_commercial_plans_and_limits_are_stable(self):
        module = ast.parse(SOURCE)
        assignment = next(n for n in module.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "LICENSE_PLANS" for t in n.targets))
        plans = ast.literal_eval(assignment.value)
        self.assertEqual(plans["base"]["price"], "79.00")
        self.assertEqual(plans["base"]["product_limit"], 100)
        self.assertEqual(plans["professional"]["price"], "129.00")
        self.assertIsNone(plans["professional"]["product_limit"])

    def test_trial_starts_only_in_email_verification_route(self):
        register_block = SOURCE[SOURCE.index('def register():'):SOURCE.index('@app.get("/verifica-email/')]
        verify_block = SOURCE[SOURCE.index('def verify_email('):SOURCE.index('@app.post("/register/google")')]
        self.assertNotIn("INSERT INTO licenze_utenti", register_block)
        self.assertIn("APP_TRIAL_DAYS", verify_block)
        self.assertIn("email_verificata=TRUE", verify_block)

    def test_ten_menu_languages_are_available(self):
        assignment = next(n for n in TREE.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SUPPORTED_MENU_LANGUAGES" for t in n.targets))
        languages = ast.literal_eval(assignment.value)
        self.assertEqual(len(languages) + 1, 10)  # nove aggiuntive più l'italiano

    def test_new_manual_accounts_start_with_initial_guide(self):
        register_block = SOURCE[SOURCE.index('def register():'):SOURCE.index('@app.get("/verifica-email/')]
        self.assertIn("guida_iniziale_vista", register_block)
        self.assertIn("FALSE, FALSE, FALSE", register_block)

    def test_guide_progress_is_based_on_saved_data(self):
        guide_block = SOURCE[SOURCE.index('def api_initial_guide('):SOURCE.index('@app.put("/api/account")')]
        self.assertIn("COUNT(*) FROM categorie", guide_block)
        self.assertIn("COUNT(*) FROM prodotti", guide_block)
        self.assertIn("COUNT(*) FROM lingue_negozio", guide_block)

    def test_billing_data_requires_tax_and_delivery_identifiers(self):
        complete = load_function("billing_data_complete", {})
        data = {"ragione_sociale":"Ristorante Srl","indirizzo":"Via Roma 1","cap":"00100","citta":"Roma","provincia":"RM","nazione":"Italia","email_amministrativa":"amministrazione@example.it","partita_iva":"12345678901","codice_fiscale":"","codice_sdi":"ABC1234","pec":""}
        self.assertTrue(complete(data))
        data["codice_sdi"] = ""
        self.assertFalse(complete(data))
        data["pec"] = "ristorante@pec.it"
        data["partita_iva"] = ""
        data["codice_fiscale"] = "RSSMRA80A01H501U"
        self.assertTrue(complete(data))

    def test_privacy_requests_are_tracked_and_export_excludes_credentials(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS richieste_privacy", SOURCE)
        self.assertIn('@app.route("/api/privacy/richieste", methods=["GET", "POST"])', SOURCE)
        export_block = SOURCE[SOURCE.index('def api_privacy_export('):SOURCE.index('@app.route("/api/guida-iniziale"')]
        self.assertIn('as_attachment=True', export_block)
        self.assertNotIn('password', export_block.lower())
        self.assertNotIn('token', export_block.lower())


class PayPalTests(unittest.TestCase):
    def test_paypal_dates_use_fallback_and_parse_iso_timestamp(self):
        parse = load_function("parse_paypal_date", {"datetime": datetime})
        fallback = date(2027, 1, 1)
        self.assertEqual(parse(None, fallback), fallback)
        self.assertEqual(parse("2027-02-03T10:15:00Z", fallback), date(2027, 2, 3))
        self.assertEqual(parse("not-a-date", fallback), fallback)

    def test_paypal_charge_includes_italian_vat(self):
        scope = {
            "Decimal": Decimal,
            "ROUND_HALF_UP": ROUND_HALF_UP,
            "LICENSE_PLANS": {"base": {"price": "79.00"}, "professional": {"price": "129.00"}},
            "VAT_RATE": Decimal("0.22"),
            "normalize_license_plan": lambda value: value,
        }
        gross = load_function("plan_price_with_vat", scope)
        self.assertEqual(gross("base"), "96.38")
        self.assertEqual(gross("professional"), "157.38")

    def test_cancel_uses_live_endpoint_and_accepts_204(self):
        response = Mock(status_code=204)
        http = SimpleNamespace(post=Mock(return_value=response))
        scope = {
            "os": os, "requests": http,
            "paypal_base_url": lambda: "https://api-m.paypal.com",
            "paypal_access_token": lambda: "token",
        }
        cancel = load_function("paypal_cancel_subscription_by_id", scope)
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": "secret"}, clear=True):
            cancel("I-TEST", "test")
        self.assertEqual(http.post.call_args.args[0], "https://api-m.paypal.com/v1/billing/subscriptions/I-TEST/cancel")

    def test_cancel_rejects_unconfirmed_response(self):
        response = Mock(status_code=404, json=Mock(return_value={"name": "RESOURCE_NOT_FOUND"}))
        scope = {"os": os, "requests": SimpleNamespace(post=Mock(return_value=response)), "paypal_base_url": lambda: "https://api-m.paypal.com", "paypal_access_token": lambda: "token"}
        cancel = load_function("paypal_cancel_subscription_by_id", scope)
        with patch.dict(os.environ, {"PAYPAL_CLIENT_ID": "id", "PAYPAL_CLIENT_SECRET": "secret"}, clear=True):
            with self.assertRaises(RuntimeError):
                cancel("I-OLD", "test")


if __name__ == "__main__":
    unittest.main()
