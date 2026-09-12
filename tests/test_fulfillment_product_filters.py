"""Controlli di regressione dei filtri prodotti nel banco evasione."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
OWNER = (ROOT / "templates" / "fulfillment_dashboard.html").read_text(encoding="utf-8")
EMPLOYEE = (ROOT / "templates" / "employee_orders.html").read_text(encoding="utf-8")


class FulfillmentProductFiltersTests(unittest.TestCase):
    def test_order_lines_include_category_metadata(self):
        endpoint = APP[APP.index("def api_ordini_evasione():"):APP.index('@app.get("/api/ordini")')]
        self.assertIn("LEFT JOIN categorie c ON c.id=p.id_categoria", endpoint)
        self.assertIn('"id_categoria": row[15]', endpoint)
        self.assertIn('"ordine_categoria": row[17]', endpoint)

    def test_both_boards_filter_and_sort_products(self):
        for page in (OWNER, EMPLOYEE):
            with self.subTest(page=page[:35]):
                self.assertIn('id="productCategory"', page)
                self.assertIn('id="productSort"', page)
                self.assertIn("visibleProducts(order)", page)
                self.assertIn("productSort.value==='categoria'", page)


if __name__ == "__main__":
    unittest.main()
