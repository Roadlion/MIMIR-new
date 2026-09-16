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
    dcf_aapl = financial_skills.run_dcf_valuation("AAPL")
    dcf_mu = financial_skills.run_dcf_valuation("MU")
    print("AAPL DCF:", json.dumps(dcf_aapl, indent=2))
    print("MU DCF:", json.dumps(dcf_mu, indent=2))
    assert dcf_aapl["current_price"] != 100.0, "AAPL price defaulted to 100.0!"
    assert dcf_aapl["margin_of_safety_pct"] != -20.31, "AAPL margin of safety is stuck at -20.31%!"
    assert dcf_mu["margin_of_safety_pct"] != -20.31, "MU margin of safety is stuck at -20.31%!"
    assert dcf_aapl["margin_of_safety_pct"] != dcf_mu["margin_of_safety_pct"], "DCF produced identical margins of safety!"
    print("[OK] DCF Valuation test passed (unique company-specific valuations verified).")

    print("\n=== TEST 2: Comps Analysis Skill ===")
    comps_v = financial_skills.run_comps_analysis("V")
    comps_mu = financial_skills.run_comps_analysis("MU")
    v_peers = [p["ticker"] for p in comps_v["peer_matrix"]]
    mu_peers = [p["ticker"] for p in comps_mu["peer_matrix"]]
    print("Visa (V) Peers:", v_peers)
    print("Micron (MU) Peers:", mu_peers)
    assert "MA" in v_peers or "AXP" in v_peers, f"Visa peers should include payment peers, got: {v_peers}"
    assert "AAPL" not in v_peers, f"Visa incorrectly mapped to Big Tech: {v_peers}"
    assert "WDC" in mu_peers or "AMAT" in mu_peers or "AMD" in mu_peers, f"Micron peers should include chip/storage peers, got: {mu_peers}"
    assert "AAPL" not in mu_peers, f"Micron incorrectly mapped to Big Tech: {mu_peers}"
    # Verify peer metrics are not all hardcoded identical 28.5
    pe_values = [p["pe_ratio"] for p in comps_v["peer_matrix"]]
    assert len(set(pe_values)) > 1, f"Peer P/E ratios are all identical: {pe_values}"
    print("[OK] Comps Analysis test passed (industry-specific peers verified).")

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
    assert port_audit["status"] == "AUDIT PASSED", f"Expected AUDIT PASSED, got: {port_audit.get('ledger_breaks')}"
    assert len(port_audit["ledger_breaks"]) == 0, f"Ledger breaks detected: {port_audit.get('ledger_breaks')}"
    print("[OK] Portfolio Audit test passed.")

    print("\n=== TEST 6: Operational Cost Auditor Skill ===")
    cost_audit = financial_skills.audit_operational_costs()
    print(json.dumps(cost_audit, indent=2))
    assert "self_funding_status" in cost_audit, "Self-funding status missing!"
    assert cost_audit.get("total_llm_api_calls", 0) > 1000, "Should use real API cost ledger records!"
    assert "DeepSeek" in cost_audit.get("cost_by_provider", {}), "DeepSeek provider missing in cost breakdown!"
    print("[OK] Operational Cost Auditor test passed (live mimir_api_cost_ledger verified).")

    print("\n=== TEST 7: Capacity-Constrained Screener Skill ===")
    screener = financial_skills.screen_capacity_constrained_assets()
    print(json.dumps(screener, indent=2))
    assert "top_candidates" in screener, "Screener candidates missing!"
    assert len(screener["top_candidates"]) > 0, "No candidates returned!"
    for cand in screener["top_candidates"]:
        assert not cand["ticker"].startswith("^"), f"Index symbol included in capacity screener: {cand['ticker']}"
    print("[OK] Capacity Screener test passed.")

    print("\n=== TEST 8: Investment Pitch Generator Skill ===")
    pitch = financial_skills.generate_pitch_pack("AAPL")
    print("Pitch Memo Excerpt:\n", pitch["investment_memo_markdown"][:300].encode('ascii', errors='ignore').decode('ascii'))
    assert "investment_memo_markdown" in pitch, "Pitch memo missing!"
    print("[OK] Pitch Pack Generator test passed.")

    print("\n=== TEST 9: Oracle Assistant Dispatcher Tool Calling ===")
    oracle_dcf = execute_oracle_tool("run_dcf_valuation", {"ticker": "AAPL"})
    assert "dcf_intrinsic_value" in oracle_dcf, "Oracle DCF tool failed!"
    oracle_comps = execute_oracle_tool("run_comps_analysis", {"ticker": "V"})
    assert "MA" in oracle_comps or "AXP" in oracle_comps, "Oracle Comps tool failed to select Visa payment peers!"
    oracle_pricing = execute_oracle_tool("query_asset_pricing", {"ticker": "AAPL", "days_back": 3})
    assert "close" in oracle_pricing, f"Oracle pricing failed for AAPL: {oracle_pricing}"
    print("[OK] Oracle Tool Dispatcher test passed.")

    print("\nALL FINANCIAL AGENTIC SKILLS TESTS PASSED SUCCESSFULLY!")

if __name__ == "__main__":
    test_financial_skills()
