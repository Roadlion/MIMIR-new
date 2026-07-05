# scripts/test_portfolio_advice.py
import sys
from pathlib import Path
from unittest.mock import patch

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.routers.portfolio import get_portfolio_advice
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

def test_advice_filtering():
    print("Testing portfolio advice logic...")
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Clean up any existing transactions for WEN and MSFT test cases
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker IN ('TEST_WEN', 'TEST_MSFT')")
    conn.commit()
    
    try:
        # 2. Insert transactions
        # - TEST_WEN: Buy 10, then Sell 10 (fully sold)
        # - TEST_MSFT: Buy 5 (retained)
        cur.execute(f"""
            INSERT INTO {settings.mimir_schema}.mimir_portfolio (ticker, order_date, buy_price, quantity, transaction_type)
            VALUES 
                ('TEST_WEN', NOW() - INTERVAL '2 days', 10.00, 10.0, 'BUY'),
                ('TEST_WEN', NOW() - INTERVAL '1 day', 12.00, 10.0, 'SELL'),
                ('TEST_MSFT', NOW() - INTERVAL '1 day', 350.00, 5.0, 'BUY')
        """)
        conn.commit()
        print("[OK] Test transactions inserted.")

        # 3. Patch send_chat_completion to inspect the prompt context
        captured_context = None
        
        def mock_send_chat_completion(messages, temperature, timeout):
            nonlocal captured_context
            for msg in messages:
                if msg["role"] == "user":
                    captured_context = msg["content"]
            return "<html>Mocked AI Advice</html>"

        with patch("backend.app.routers.portfolio.send_chat_completion", side_effect=mock_send_chat_completion):
            res = get_portfolio_advice()
            
        print(f"API result: {res}")
        assert captured_context is not None, "Did not capture LLM user prompt"
        
        # Verify that TEST_WEN is NOT in the prompt context but TEST_MSFT is
        print("Checking captured context...")
        assert "TEST_MSFT" in captured_context, "TEST_MSFT should be in the context"
        assert "TEST_WEN" not in captured_context, "TEST_WEN should NOT be in the context since it was fully sold"
        
        # Verify enriched fields presence
        assert "unrealized_profit_loss" in captured_context, "unrealized_profit_loss should be in the context"
        assert "realized_profit_loss" in captured_context, "realized_profit_loss should be in the context"
        assert "total_profit_loss" in captured_context, "total_profit_loss should be in the context"
        assert "recent_price_movement" in captured_context, "recent_price_movement should be in the context"
        assert "change_5d_pct" in captured_context, "change_5d_pct should be in the context"
        
        print("[OK] Enriched portfolio context verification passed!")

    finally:
        # 4. Clean up test data
        cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_portfolio WHERE ticker IN ('TEST_WEN', 'TEST_MSFT')")
        conn.commit()
        cur.close()
        conn.close()
        print("[OK] Cleaned up test data.")

if __name__ == "__main__":
    try:
        test_advice_filtering()
        print("[OK] ALL ADVICE TESTS PASSED.")
    except Exception as e:
        print(f"[FAIL] Verification failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
