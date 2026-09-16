import unittest
import csv
import io
import json
from fastapi.testclient import TestClient
from backend.app.main import app

class TestPortfolioExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_export_portfolio_csv_format(self):
        """Verify portfolio CSV export returns 200, valid CSV headers, and Content-Disposition."""
        res = self.client.get("/api/v1/portfolio/export?format=csv")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("content-type", ""))
        self.assertIn("attachment; filename=", res.headers.get("content-disposition", ""))
        
        # Parse CSV
        content = res.text
        self.assertTrue(content.startswith("\ufeff"), "CSV should start with UTF-8 BOM")
        reader = csv.reader(io.StringIO(content.lstrip("\ufeff")))
        headers = next(reader)
        expected_headers = [
            "Transaction ID", "Order Date (GMT+7)", "Ticker", "Action",
            "Quantity", "Price ($)", "Gross Total ($)", "Brokerage Fee ($)",
            "Regulatory Fee ($)", "Other Fee ($)", "Total Fees ($)", "Net Total ($)",
            "Source", "Created At (GMT+7)"
        ]
        self.assertEqual(headers, expected_headers)

    def test_export_portfolio_json_format(self):
        """Verify portfolio JSON export returns 200, valid JSON array, and expected fields."""
        res = self.client.get("/api/v1/portfolio/export?format=json")
        self.assertEqual(res.status_code, 200)
        self.assertIn("application/json", res.headers.get("content-type", ""))
        self.assertIn("attachment; filename=", res.headers.get("content-disposition", ""))

        data = res.json()
        self.assertIsInstance(data, list)
        if data:
            item = data[0]
            required_keys = {
                "id", "order_date", "ticker", "transaction_type",
                "quantity", "price", "gross_total", "brokerage_fee",
                "regulatory_fee", "other_fee", "total_fees", "net_total",
                "source", "created_at"
            }
            self.assertTrue(required_keys.issubset(item.keys()))

    def test_export_portfolio_invalid_format(self):
        """Verify requesting an unsupported format returns 400 Bad Request."""
        res = self.client.get("/api/v1/portfolio/export?format=xml")
        self.assertEqual(res.status_code, 400)

    def test_export_portfolio_filter_ticker(self):
        """Verify filtering by ticker returns only transactions for that ticker."""
        res = self.client.get("/api/v1/portfolio/export?format=json&ticker=DLNG")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for item in data:
            self.assertEqual(item["ticker"], "DLNG")

    def test_export_portfolio_filter_action(self):
        """Verify filtering by transaction_type returns only BUYs or SELLs."""
        res = self.client.get("/api/v1/portfolio/export?format=json&transaction_type=SELL")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for item in data:
            self.assertEqual(item["transaction_type"], "SELL")

    def test_export_paper_trades_csv(self):
        """Verify paper trading deals CSV export returns 200 and valid headers."""
        res = self.client.get("/api/v1/paper-trading/export?format=csv")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/csv", res.headers.get("content-type", ""))
        self.assertIn("attachment; filename=", res.headers.get("content-disposition", ""))

        content = res.text
        self.assertTrue(content.startswith("\ufeff"), "CSV should start with UTF-8 BOM")
        reader = csv.reader(io.StringIO(content.lstrip("\ufeff")))
        headers = next(reader)
        self.assertIn("Ticket", headers)
        self.assertIn("Realized P&L ($)", headers)

if __name__ == "__main__":
    unittest.main()
