"""Controlli di regressione per il ruolo dipendente limitato agli ordini."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
ACCOUNT = (ROOT / "templates" / "sections" / "account.html").read_text(encoding="utf-8")
EMPLOYEE_VIEW = (ROOT / "templates" / "employee_orders.html").read_text(encoding="utf-8")


class EmployeeAccessTests(unittest.TestCase):
    def test_employee_session_has_narrow_allowlist(self):
        start = APP.index("def restrict_employee_access():")
        end = APP.index('@app.get("/manifest.webmanifest")', start)
        guard = APP[start:end]
        self.assertIn('"api_ordini_evasione"', guard)
        self.assertIn('"employee_orders"', guard)
        self.assertIn('"api_crea_ordine_menu"', guard)
        self.assertIn('request.path != "/api/ordini/manuale"', guard)
        self.assertNotIn('"api_aggiorna_ordine"', guard)
        self.assertNotIn('"dashboard_user"', guard)
        self.assertIn("d.attivo", guard)
        self.assertIn("license_is_active", guard)

    def test_employee_view_can_add_but_not_change_existing_orders(self):
        self.assertIn("/api/ordini/evasione", EMPLOYEE_VIEW)
        self.assertIn("/api/ordini/manuale", EMPLOYEE_VIEW)
        self.assertNotIn("method:'PATCH'", EMPLOYEE_VIEW)
        self.assertIn("setInterval(load,15000)", EMPLOYEE_VIEW)

    def test_owner_can_manage_employees(self):
        self.assertIn("Accessi dipendenti", ACCOUNT)
        self.assertIn("/api/dipendenti", ACCOUNT)
        self.assertIn("password di almeno 6 caratteri", APP)


if __name__ == "__main__":
    unittest.main()
