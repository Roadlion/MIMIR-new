# scripts/test_financial_skills.py
import os
import sys
import json

# Adjust path so we can import backend modules
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics import financial_skills
from backend.app.sentiment.agent_tools import execute_oracle_tool

def test_financial_skills():
    print("=== TEST 1: DCF Valuation Skill ===")
    dcf = financial_skills.run_dcf_valuation("AAPL")
    print(json.dumps(dcf, indent=2))
    assert "dcf_intrinsic_value" in dcf, "DCF intrinsic value missing!"
    assert "margin_of_safety_pct" in dcf, "Margin of safety missing!"
    print("[OK] DCF Valuation test passed.")

    print("\n=== TEST 2: Comps Analysis Skill ===")
    comps = financial_skills.run_comps_analysis("NVDA")
    print(json.dumps(comps, indent=2))
    assert "peer_matrix" in comps, "Comps peer matrix missing!"
    print("[OK] Comps Analysis test passed.")

    print("\n=== TEST 3: LBO Model Skill ===")
    lbo = financial_skills.run_lbo_analysis("TSLA")
    print(json.dumps(lbo, indent=2))
    assert "projected_5yr_irr_pct" in lbo, "LBO IRR missing!"
    print("[OK] LBO Model test passed.")

    print("\n=== TEST 4: Earnings Reviewer Skill ===")
    earnings = financial_skills.review_earnings("MSFT")
    print(json.dumps(earnings, indent=2))
    assert "pead_momentum_signal" in earnings, "PEAD momentum missing!"
    print("[OK] Earnings Reviewer test passed.")

    print("\n=== TEST 5: Portfolio Audit Skill ===")
    port_audit = financial_skills.reconcile_portfolio_audit()
    print(json.dumps(port_audit, indent=2))
    assert "status" in port_audit, "Portfolio audit status missing!"
    print("[OK] Portfolio Audit test passed.")

    print("\n=== TEST 6: Operational Cost Auditor Skill ===")
    cost_audit = financial_skills.audit_operational_costs()
    print(json.dumps(cost_audit, indent=2))
    assert "self_funding_status" in cost_audit, "Self-funding status missing!"
    print("[OK] Operational Cost Auditor test passed.")

    print("\n=== TEST 7: Capacity-Constrained Screener Skill ===")
    screener = financial_skills.screen_capacity_constrained_assets()
    print(json.dumps(screener, indent=2))
    assert "top_candidates" in screener, "Screener candidates missing!"
    print("[OK] Capacity Screener test passed.")

    print("\n=== TEST 8: Investment Pitch Generator Skill ===")
    pitch = financial_skills.generate_pitch_pack("AAPL")
    print("Pitch Memo Excerpt:\n", pitch["investment_memo_markdown"][:300].encode('ascii', errors='ignore').decode('ascii'))
    assert "investment_memo_markdown" in pitch, "Pitch memo missing!"
    print("[OK] Pitch Pack Generator test passed.")

    print("\n=== TEST 9: Oracle Assistant Dispatcher Tool Calling ===")
    oracle_res = execute_oracle_tool("run_dcf_valuation", {"ticker": "AAPL"})
    print("Oracle Tool Res:", oracle_res[:200])
    assert "dcf_intrinsic_value" in oracle_res, "Oracle DCF tool failed!"
    print("[OK] Oracle Tool Dispatcher test passed.")

    print("\nALL FINANCIAL AGENTIC SKILLS TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_financial_skills()
