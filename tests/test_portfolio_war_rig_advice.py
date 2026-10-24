# tests/test_portfolio_war_rig_advice.py
import unittest
from unittest.mock import patch, MagicMock
from backend.app.routers.portfolio import fetch_portfolio_price_trends, get_portfolio_advice

class TestPortfolioWarRigAdvice(unittest.TestCase):
    def test_fetch_portfolio_price_trends_war_rig_bounds(self):
        """Verify that fetch_portfolio_price_trends returns War Rig microstructure bounds."""
        trends = fetch_portfolio_price_trends(["VOO"])
        self.assertIn("VOO", trends)
        data = trends["VOO"]
        
        self.assertIn("technical_analysis", data)
        ta = data["technical_analysis"]
        
        # Verify War Rig microstructure attributes
        self.assertIn("rsi_14", ta)
        self.assertIn("dma_50", ta)
        self.assertIn("dma_200", ta)
        self.assertIn("volume_ratio", ta)
        self.assertIn("support", ta)
        self.assertIn("resistance", ta)
        self.assertIn("atr_14", ta)
        self.assertIn("volatility_regime", ta)
        self.assertIn("ou_zscore", ta)
        self.assertIn("execution_bounds", ta)
        
        bounds = ta["execution_bounds"]
        self.assertIn("trigger_price", bounds)
        self.assertIn("stop_loss", bounds)
        self.assertIn("target_price", bounds)
        self.assertIn("risk_reward_ratio", bounds)
        self.assertIn("upside_pct", bounds)
        self.assertIn("downside_pct", bounds)
        self.assertGreaterEqual(bounds["risk_reward_ratio"], 2.49)

    @patch("backend.app.routers.portfolio.send_chat_completion")
    def test_get_portfolio_advice_prompt_war_rig_structure(self, mock_llm):
        """Verify prompt structure feeds War Rig 3-cylinder context and decommissions old profit strategies."""
        mock_llm.return_value = """
<div class="mb-8">
  <h3>🛡️ Portfolio Health & 3-Cylinder Diagnostic Matrix</h3>
  <table><tbody><tr><td>MU</td><td>Stop: $980 | Target: $1220</td></tr></tbody></table>
</div>
<div class="mb-8">
  <h3>⚔️ War Rig Convergence Alpha Opportunities</h3>
</div>
<div class="mb-8">
  <h3>🌍 Macro Regime & Sector Transmission</h3>
</div>
<div class="mb-6">
  <h3>🎯 Tactical Execution Deck & Asymmetric Rebalancing</h3>
</div>
"""
        res = get_portfolio_advice(current_user={"id": 1})
        self.assertIn("advice", res)
        advice_html = res["advice"]
        
        # Verify Section 4 has Tactical Execution Deck
        self.assertIn("Tactical Execution Deck", advice_html)
        
        # Verify Section 4 does NOT have legacy "Alternative MIMIR Profit Strategies"
        self.assertNotIn("Alternative MIMIR Profit Strategies", advice_html)
        
        # Verify LLM was called with War Rig system prompt and enriched user prompt
        self.assertTrue(mock_llm.called)
        call_args = mock_llm.call_args[1]
        messages = call_args["messages"]
        system_msg = messages[0]["content"]
        user_msg = messages[1]["content"]
        
        # System prompt assertions
        self.assertIn("War Rig Alpha Transmission Engine", system_msg)
        self.assertIn("Cylinder 1", system_msg)
        self.assertIn("Cylinder 2", system_msg)
        self.assertIn("Cylinder 3", system_msg)
        
        self.assertIn("generated_at", res)
        
        # User prompt context assertions
        self.assertIn("pipeline_snapshot_timestamp", user_msg)
        self.assertIn("portfolio_holdings", user_msg)
        self.assertIn("portfolio_sentiment_and_catalysts", user_msg)
        self.assertIn("live_war_rig_convergence_signals", user_msg)
        self.assertIn("asymmetric_execution_bounds", user_msg)
        self.assertIn("nitrous_options_directive", user_msg)
        self.assertIn("BUY or SELL PUTS", user_msg)
        self.assertIn("SPECIFIC NUMERICAL STRIKE PRICES", user_msg)
        self.assertIn("Nitrous Options Execution", user_msg)
        self.assertIn("Tactical Execution Deck & Asymmetric Rebalancing", user_msg)
        self.assertNotIn("Alternative MIMIR Profit Strategies</h3>", user_msg)

    def test_nitrous_options_directives(self):
        """Verify Nitrous Pod generates exact buy/sell put directives with explicit strike prices."""
        from backend.app.analytics.war_rig_nitrous import get_nitrous_bridge
        bridge = get_nitrous_bridge()

        # Test portfolio holding directive (Normal runner -> Mode B2 Bull Put Credit Spread)
        directive = bridge.generate_holding_nitrous_directive(
            ticker="AAPL",
            spot_price=225.0,
            stop_loss=215.0,
            target_price=245.0,
            atr_14=4.5
        )
        self.assertEqual(directive["primary_action"], "SELL PUT")
        self.assertIn("SELL", directive["action_summary"])
        self.assertIn("PUT", directive["action_summary"])
        self.assertIn("put_credit_spread_strikes", directive)
        pcs = directive["put_credit_spread_strikes"]
        self.assertLessEqual(pcs["short_put_strike"], 225.0)
        self.assertLess(pcs["long_put_strike"], pcs["short_put_strike"])
        self.assertGreater(pcs["net_credit"], 0.0)

        # Test portfolio holding facing earnings danger -> Protective Put (BUY PUT)
        earnings_directive = bridge.generate_holding_nitrous_directive(
            ticker="NVDA",
            spot_price=120.0,
            stop_loss=112.0,
            target_price=138.0,
            atr_14=3.5,
            earnings_calendar={"days_until": 7, "earnings_time": "AMC"}
        )
        self.assertEqual(earnings_directive["primary_action"], "BUY PUT")
        self.assertIn("BUY", earnings_directive["action_summary"])
        self.assertIn("PUT", earnings_directive["action_summary"])
        self.assertIn("protective_put_strike", earnings_directive)
        self.assertGreater(earnings_directive["protective_put_strike"]["strike"], 0.0)

        # Test War Rig convergence pick deployment
        deployment = bridge.generate_nitrous_deployment({
            "ticker": "TSLA",
            "trigger_price": 250.0,
            "stop_loss": 235.0,
            "target_price": 280.0,
            "conviction_score": 0.85
        })
        self.assertIn("nitrous_options_directive", deployment)
        opt_dir = deployment["nitrous_options_directive"]
        self.assertIsNotNone(opt_dir)
        self.assertIn(opt_dir["primary_action"], ["SELL PUT", "BUY CALL", "BUY PUT"])
        self.assertIn("action_summary", opt_dir)
        self.assertIn("execution_order", opt_dir)
        self.assertGreater(len(opt_dir["legs"]), 0)


if __name__ == "__main__":
    unittest.main()

