"""Controlli di regressione per il ruolo dipendente limitato agli ordini."""
from pathlib import Path
import unittest
import ast
import copy
from types import SimpleNamespace
from flask import Flask, request, session, redirect, render_template


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
ACCOUNT = (ROOT / "templates" / "sections" / "account.html").read_text(encoding="utf-8")
EMPLOYEE_VIEW = (ROOT / "templates" / "employee_orders.html").read_text(encoding="utf-8")
FLASK = Flask(__name__)
FLASK.secret_key = "test-only"


class EmployeeAccessTests(unittest.TestCase):
    def test_existing_shared_email_uses_employee_password_when_owner_password_differs(self):
        class Cursor:
            query = ""
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, query, _params): self.query = query
            def fetchone(self):
                if "FROM utenti u" in self.query:
                    return (1, "Owner", "owner-secret", False, "attiva", None, True)
                if "FROM dipendenti_negozio d" in self.query:
                    return (2, 7, "Employee", "employee-secret", True, "attiva", None)
                raise AssertionError(self.query)
        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass
        node = copy.deepcopy(next(item for item in ast.parse(APP).body if isinstance(item, ast.FunctionDef) and item.name == "login"))
        node.decorator_list = []
        scope = {"request": request, "session": session, "redirect": redirect, "render_template": render_template,
                 "psycopg2": SimpleNamespace(connect=lambda **_: Connection()), "build_db_config": lambda: {},
                 "url_for": lambda name: "/dipendenti/ordini", "login_is_limited": lambda _: False,
                 "verify_password": lambda password, stored: password == stored,
                 "license_is_active": lambda *_: True, "clear_login_failures": lambda _: None,
                 "record_login_failure": lambda _: None, "google_enabled": lambda: False}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), scope)
        with FLASK.test_request_context("/login", method="POST", data={"email":"same@example.it", "password":"employee-secret"}):
            response = scope["login"]()
            self.assertEqual(response.location, "/dipendenti/ordini")
            self.assertEqual(session["employee_shop_id"], 7)

    def test_employee_session_has_narrow_allowlist(self):
        start = APP.index("def restrict_employee_access():")
        end = APP.index('@app.get("/manifest.webmanifest")', start)
        guard = APP[start:end]
        self.assertIn('"api_ordini_evasione"', guard)
        self.assertIn('"employee_orders"', guard)
        self.assertIn('"api_crea_ordine_menu"', guard)
        self.assertIn('request.path != "/api/ordini/manuale"', guard)
        self.assertIn('"api_aggiorna_ordine"', guard)
        self.assertNotIn('"dashboard_user"', guard)
        self.assertIn("d.attivo", guard)
        self.assertIn("license_is_active", guard)

    def test_employee_view_can_add_complete_and_cancel_orders(self):
        self.assertIn("/api/ordini/evasione", EMPLOYEE_VIEW)
        self.assertIn("/api/ordini/manuale", EMPLOYEE_VIEW)
        self.assertIn("method:'PATCH'", EMPLOYEE_VIEW)
        self.assertIn("['evaso','Segna evaso']", EMPLOYEE_VIEW)
        self.assertIn("['annullato','Annulla']", EMPLOYEE_VIEW)
        self.assertIn("Annullare l’ordine #", EMPLOYEE_VIEW)
        self.assertIn("setInterval(load,15000)", EMPLOYEE_VIEW)
        self.assertIn('id="summaryView"', EMPLOYEE_VIEW)
        self.assertIn('id="ordersView"', EMPLOYEE_VIEW)
        self.assertIn("quantityMode='total'", EMPLOYEE_VIEW)
        self.assertIn('id="period"', EMPLOYEE_VIEW)
        self.assertIn('id="grouping"', EMPLOYEE_VIEW)

    def test_owner_can_manage_employees(self):
        self.assertIn("Accessi dipendenti", ACCOUNT)
        self.assertIn("/api/dipendenti", ACCOUNT)
        self.assertIn("password di almeno 6 caratteri", APP)


if __name__ == "__main__":
    unittest.main()
