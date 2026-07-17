# scripts/test_portfolio_fees_and_edit.py
import sys
from pathlib import Path
from datetime import datetime, timezone

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()
client = TestClient(app)

def clean_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker IN ('FEESTEST', 'FEESTEST2')")
    conn.commit()
    cur.close()
    conn.close()

def test_add_and_edit_with_fees():
    print("Cleaning up database...")
    clean_db()

    print("1. Creating a BUY transaction for FEESTEST...")
    # Add a BUY order with fees
    buy_payload = {
        "ticker": "FEESTEST",
        "order_date": "2026-07-15T12:00:00Z",
        "buy_price": 100.0,
        "quantity": 10.0,
        "transaction_type": "BUY",
        "brokerage_fee": 5.0,
        "regulatory_fee": 1.0,
        "other_fee": 2.0
    }
    response = client.post("/api/v1/portfolio", json=buy_payload)
    assert response.status_code == 200, f"Failed to add transaction: {response.text}"
    buy_tx = response.json()
    assert buy_tx["brokerage_fee"] == 5.0
    assert buy_tx["regulatory_fee"] == 1.0
    assert buy_tx["other_fee"] == 2.0
    print("[OK] BUY transaction created with fees.")

    print("2. Checking portfolio calculations for BUY fees (increases cost basis)...")
    # Fetch portfolio
    portfolio_res = client.get("/api/v1/portfolio")
    assert portfolio_res.status_code == 200
    portfolio_data = portfolio_res.json()
    holding = portfolio_data["holdings"]["FEESTEST"]
    # cost basis should include fees: 10 * 100 + 5 + 1 + 2 = 1008.0
    # avg_buy_price = 100.8
    assert float(holding["total_cost"]) == 1008.0
    assert float(holding["avg_buy_price"]) == 100.8
    print("[OK] Cost basis calculated correctly.")

    print("3. Creating a SELL transaction for FEESTEST with fees...")
    # Add a SELL order with fees
    sell_payload = {
        "ticker": "FEESTEST",
        "order_date": "2026-07-15T13:00:00Z",
        "buy_price": 150.0,
        "quantity": 5.0,
        "transaction_type": "SELL",
        "brokerage_fee": 4.0,
        "regulatory_fee": 0.5,
        "other_fee": 1.5
    }
    response = client.post("/api/v1/portfolio", json=sell_payload)
    assert response.status_code == 200, f"Failed to add SELL: {response.text}"
    sell_tx = response.json()
    assert sell_tx["brokerage_fee"] == 4.0
    assert sell_tx["regulatory_fee"] == 0.5
    assert sell_tx["other_fee"] == 1.5
    print("[OK] SELL transaction created with fees.")

    print("4. Checking portfolio calculations for SELL fees (decreases realized P&L)...")
    portfolio_res = client.get("/api/v1/portfolio")
    portfolio_data = portfolio_res.json()
    holding = portfolio_data["holdings"]["FEESTEST"]
    # remaining quantity should be 5
    assert float(holding["quantity"]) == 5.0
    # realized P&L = qty * (sell_price - avg_buy_price) - sell_fees
    # = 5 * (150 - 100.8) - (4.0 + 0.5 + 1.5)
    # = 5 * 49.2 - 6 = 246.0 - 6 = 240.0
    assert float(holding["realized_pl"]) == 240.0
    print("[OK] Realized P&L calculated correctly.")

    print("5. Editing the BUY transaction...")
    # Edit the buy transaction: change price to 110, brokerage to 10
    edit_payload = {
        "ticker": "FEESTEST",
        "order_date": "2026-07-15T12:00:00Z",
        "buy_price": 110.0,
        "quantity": 10.0,
        "transaction_type": "BUY",
        "brokerage_fee": 10.0,
        "regulatory_fee": 1.0,
        "other_fee": 2.0
    }
    edit_res = client.put(f"/api/v1/portfolio/{buy_tx['id']}", json=edit_payload)
    assert edit_res.status_code == 200, f"Edit failed: {edit_res.text}"
    updated_tx = edit_res.json()
    assert updated_tx["buy_price"] == 110.0
    assert updated_tx["brokerage_fee"] == 10.0
    print("[OK] Edit transaction succeeded.")

    print("6. Verifying calculations updated after edit...")
    portfolio_res = client.get("/api/v1/portfolio")
    portfolio_data = portfolio_res.json()
    holding = portfolio_data["holdings"]["FEESTEST"]
    # new buy cost = 10 * 110 + 10 + 1 + 2 = 1113.0
    # new avg_buy_price = 111.3
    # realized P&L = 5 * (150 - 111.3) - 6 = 5 * 38.7 - 6 = 193.5 - 6 = 187.5
    assert float(holding["avg_buy_price"]) == 111.3
    assert float(holding["realized_pl"]) == 187.5
    print("[OK] Post-edit calculations verified successfully.")

    print("7. Testing inventory constraint on edit...")
    # Try to edit the BUY order quantity to 4 (which is less than the 5 sold, resulting in negative running quantity)
    invalid_edit = {
        "ticker": "FEESTEST",
        "order_date": "2026-07-15T12:00:00Z",
        "buy_price": 110.0,
        "quantity": 4.0,
        "transaction_type": "BUY",
        "brokerage_fee": 10.0,
        "regulatory_fee": 1.0,
        "other_fee": 2.0
    }
    invalid_res = client.put(f"/api/v1/portfolio/{buy_tx['id']}", json=invalid_edit)
    assert invalid_res.status_code == 400
    assert "Proposed changes would result in a negative holding quantity" in invalid_res.json()["detail"]
    print("[OK] Inventory constraint correctly enforced on edit.")

    print("Cleaning up database...")
    clean_db()
    print("[SUCCESS] All fees and edit tests passed!")

if __name__ == "__main__":
    try:
        test_add_and_edit_with_fees()
    except Exception as e:
        print(f"[FAIL] Verification failed: {e}")
        clean_db()
        sys.exit(1)
