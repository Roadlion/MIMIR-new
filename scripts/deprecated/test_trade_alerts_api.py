# scripts/test_trade_alerts_api.py
import os
import sys
import json
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()
client = TestClient(app)

def setup_test_signal():
    """Inserts a mock pending trade signal for testing."""
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Delete any existing pending signals for MSFT to avoid constraint checks
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE ticker = 'MSFT'")
        
        sql = f"""
            INSERT INTO {settings.mimir_schema}.mimir_trade_signals 
            (ticker, signal_type, trigger_price, rsi_value, sentiment_score, support_level, resistance_level, reason, status)
            VALUES ('MSFT', 'BUY', 400.0, 32.5, 0.45, 395.0, 415.0, 'Test BUY signal for API verification', 'PENDING')
            RETURNING id
        """
        cur.execute(sql)
        signal_id = cur.fetchone()[0]
        conn.commit()
        return signal_id
    finally:
        cur.close()
        conn.close()

def clean_test_records(signal_id):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_trade_signals WHERE id = %s", (signal_id,))
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = 'MSFT'")
        conn.commit()
    finally:
        cur.close()
        conn.close()

def run_api_tests():
    print("--- Testing Trade Alerts API Endpoints ---")
    
    # 1. Setup mock signal
    signal_id = setup_test_signal()
    print(f"Created mock pending signal with ID: {signal_id}")
    
    try:
        # 2. Get pending alerts list
        print("Testing GET /api/v1/alerts/pending...")
        response = client.get("/api/v1/alerts/pending")
        assert response.status_code == 200
        pending_list = response.json()
        assert len(pending_list) >= 1
        
        # Verify MSFT is in the pending list
        msft_signals = [s for s in pending_list if s["ticker"] == "MSFT"]
        assert len(msft_signals) > 0
        print(f"Verified GET endpoint. Found MSFT in pending signals.")
        
        # 3. Approve the signal
        print(f"Testing POST /api/v1/alerts/{signal_id}/approve...")
        payload = {"quantity": 15.0}
        response = client.post(f"/api/v1/alerts/{signal_id}/approve", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "APPROVED"
        assert result["acted_at"] is not None
        print("Alert approved successfully.")
        
        # Verify transaction created in Shadow Portfolio
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker = 'MSFT'")
        tx = cur.fetchone()
        assert tx is not None, "Shadow Portfolio transaction not created!"
        print(f"Verified transaction in Shadow Portfolio: Ticker: {tx[1]}, Price: {tx[3]}, Quantity: {tx[4]}")
        cur.close()
        conn.close()
        
    finally:
        # Clean up
        clean_test_records(signal_id)
        print("Cleaned up test records from database.")
        
    print("\nAll trade alerts API router tests passed successfully!")

if __name__ == "__main__":
    run_api_tests()
