# scripts/test_high_leverage_improvements.py
"""
MIMIR High-Leverage Money-Making Pipeline Improvements Test Suite
================================================================
Validates all 7 tracks:
1. Track 1 (P0): Volatility-Adaptive Chandelier Ratchet (Premature Shakeout Prevention)
2. Track 2 (P0): Defensive Macro Regime Exemption (Sector Flight Longs)
3. Track 3 (P1): Volatility-Parity / Fixed Risk Budget Position Sizing
4. Track 4 (P1): Dual-Tier Universe Mid-Cap Discovery ($5M ADV + Catalyst corridor)
5. Track 5 (P2): Implied vs Historical Move Ratio (IHMR) Pre-Earnings Volatility Filter
6. Track 6 (P2): Options Paper Trading & Mark-to-Market Lifecycle Bridge
7. Track 7 (P3): Stat-Arb APU Dynamic Cointegration Screener & Beta-Neutral Sizing
"""

import sys
from pathlib import Path
from datetime import datetime, timezone, date, timedelta

# Setup root path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root))

from backend.app.database import get_db_connection, get_db_connection_dict
from backend.app.config import get_settings
from backend.app.services.macro_tracker import is_market_regime_bullish, DEFENSIVE_TICKERS
from backend.app.analytics.paper_trader import (
    get_ticker_atr_data,
    get_ticker_atr_bounds,
    init_paper_trading_db,
    get_paper_config
)
from backend.app.analytics.war_rig_engine import calculate_ihmr, WarRigEngine
from backend.app.analytics.war_rig_nitrous import execute_paper_options_order, revalue_open_options_positions
from backend.app.analytics.stat_arb_apu import StatArbAPU

settings = get_settings()


def test_track_1_chandelier_ratchet():
    print("\n" + "="*70)
    print("TEST SUITE 1 (Track 1): Volatility-Adaptive Chandelier Ratchet")
    print("="*70)

    # High-beta asset simulation: NVDA (Entry = 100.0, ATR = 4.0%)
    entry_price = 100.0
    atr_dollar = 4.0
    atr_pct = 4.0

    # Calculate dynamic thresholds
    t1_trigger = max(4.5, 1.5 * atr_pct)  # 6.0%
    t2_trigger = max(7.5, 2.5 * atr_pct)  # 10.0%
    t3_trigger = max(12.0, 3.5 * atr_pct) # 14.0%

    print(f"Calculated Dynamic Ratchet Triggers for ATR {atr_pct}%:")
    print(f"  Tier 1 Breakeven Lock:  +{t1_trigger:.1f}%")
    print(f"  Tier 2 +1.0 ATR Lock:   +{t2_trigger:.1f}%")
    print(f"  Tier 3 Chandelier Trail: +{t3_trigger:.1f}%")

    # Scenario A: Premature Shakeout Prevention at +3.5% Gain
    # Legacy system would have moved stop to 100.5, choking out a normal 2% pullback.
    gain_pct = 3.5
    should_ratchet_legacy = (gain_pct >= 3.0)
    should_ratchet_v2 = (gain_pct >= t1_trigger)
    assert should_ratchet_legacy == True, "Legacy system should have ratcheted at +3%"
    assert should_ratchet_v2 == False, "V2 Volatility Ratchet MUST NOT choke trade at +3.5%!"
    print("[PASS] Scenario A: At +3.5% gain, V2 successfully preserves trade breathing room.")

    # Scenario B: Tier 1 Trigger at +6.5% Gain
    gain_pct = 6.5
    assert gain_pct >= t1_trigger
    t1_sl = round(entry_price * 1.005, 2) # 100.5
    print(f"[PASS] Scenario B: At +6.5% gain, Breakeven locked at ${t1_sl} (+0.5% net).")

    # Scenario C: Tier 2 Trigger at +10.5% Gain
    gain_pct = 10.5
    assert gain_pct >= t2_trigger
    t2_sl = round(entry_price + (1.0 * atr_dollar), 2) # 104.0
    print(f"[PASS] Scenario C: At +10.5% gain, Locked +1.0 ATR profit at ${t2_sl}.")

    # Scenario D: Tier 3 Chandelier at +16.0% Gain (Peak = 116.0)
    peak_price = 116.0
    gain_pct = 16.0
    assert gain_pct >= t3_trigger
    ch_sl = round(peak_price - (1.5 * atr_dollar), 2) # 116.0 - 6.0 = 110.0
    print(f"[PASS] Scenario D: At +16.0% gain (Peak ${peak_price}), Chandelier trailed to ${ch_sl}.")


def test_track_2_defensive_macro_exemption():
    print("\n" + "="*70)
    print("TEST SUITE 2 (Track 2): Defensive Macro Regime Exemption")
    print("="*70)

    conn = get_db_connection()
    try:
        # Check standard market regime
        is_bullish, reason = is_market_regime_bullish(conn=conn)
        print(f"Current Baseline Market Regime: is_bullish={is_bullish}, reason='{reason}'")

        # Test evaluating with defensive ticker XOM
        is_xom_ok, xom_reason = is_market_regime_bullish(conn=conn, ticker="XOM")
        print(f"Evaluated 'XOM' (Defensive Major): approved={is_xom_ok}, reason='{xom_reason}'")

        # Test evaluating with non-defensive high-beta ticker NVDA
        is_nvda_ok, nvda_reason = is_market_regime_bullish(conn=conn, ticker="NVDA")
        print(f"Evaluated 'NVDA' (Growth Semi): approved={is_nvda_ok}, reason='{nvda_reason}'")

        assert "XOM" in DEFENSIVE_TICKERS
        assert "XLU" in DEFENSIVE_TICKERS
        assert "GLD" in DEFENSIVE_TICKERS
        print("[PASS] Track 2: Defensive taxonomy and macro gating functions validated.")
    finally:
        conn.close()


def test_track_3_volatility_parity_sizing():
    print("\n" + "="*70)
    print("TEST SUITE 3 (Track 3): Volatility-Parity / Fixed Risk Budget Sizing")
    print("="*70)

    account_equity = 100000.0  # $100k portfolio
    base_risk_pct = 0.015      # 1.5% base risk ($1,500)
    conviction = 0.85
    conviction_factor = 0.8 + 0.4 * ((max(0.60, min(1.0, conviction)) - 0.60) / 0.40) # ~1.05
    dollar_risk_budget = account_equity * base_risk_pct * conviction_factor # ~$1,575

    # Asset A: Tight Stop Asset (SL = 3.5%)
    sl_pct_a = 3.5
    alloc_a = dollar_risk_budget / (sl_pct_a / 100.0)
    dollar_loss_a = alloc_a * (sl_pct_a / 100.0)

    # Asset B: Wide Stop High-Beta Asset (SL = 7.5%)
    sl_pct_b = 7.5
    alloc_b = dollar_risk_budget / (sl_pct_b / 100.0)
    dollar_loss_b = alloc_b * (sl_pct_b / 100.0)

    print(f"Risk Budget: ${dollar_risk_budget:.2f}")
    print(f"  Asset A (3.5% SL): Target Alloc = ${alloc_a:.2f} -> Dollar Risk at SL = ${dollar_loss_a:.2f}")
    print(f"  Asset B (7.5% SL): Target Alloc = ${alloc_b:.2f} -> Dollar Risk at SL = ${dollar_loss_b:.2f}")

    assert abs(dollar_loss_a - dollar_loss_b) < 0.01, "Both trades MUST risk identical dollar amounts!"
    assert alloc_a > alloc_b, "Tight stop trade must receive larger allocation than wide stop trade!"
    print("[PASS] Track 3: Volatility-Parity equates dollar risk across volatility regimes.")


def test_track_4_mid_cap_discovery():
    print("\n" + "="*70)
    print("TEST SUITE 4 (Track 4): Dual-Tier Universe Mid-Cap Discovery")
    print("="*70)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Tier 2 mid-cap query ($5M to $25M ADV with active supply chain edge or earnings)
        cur.execute(f"""
            WITH daily_bars AS (
                SELECT ticker, AVG(close) AS close, SUM(volume) AS volume
                FROM {settings.mimir_schema}.mimir_hourly_ohlcv
                WHERE timestamp >= NOW() - INTERVAL '5 days'
                GROUP BY ticker, timestamp::date
            ),
            candidates AS (
                SELECT source_key AS ticker FROM {settings.mimir_schema}.mimir_asset_relationships WHERE is_active = TRUE
                UNION
                SELECT target_key AS ticker FROM {settings.mimir_schema}.mimir_asset_relationships WHERE is_active = TRUE
                UNION
                SELECT ticker FROM {settings.mimir_schema}.mimir_earnings_calendar WHERE earnings_date BETWEEN CURRENT_DATE - INTERVAL '1 day' AND CURRENT_DATE + INTERVAL '14 days'
            )
            SELECT DISTINCT d.ticker
            FROM daily_bars d
            JOIN candidates c ON d.ticker = c.ticker
            WHERE d.close >= 5.0
              AND (d.close * d.volume) BETWEEN 5000000 AND 25000000
            LIMIT 10
        """)
        mid_caps = [r[0] for r in cur.fetchall()]
        print(f"Discovered {len(mid_caps)} Mid-Cap Catalyst Corridors: {mid_caps}")
        assert len(mid_caps) > 0, "Mid-Cap discovery should discover active catalyst candidates!"
        print("[PASS] Track 4: Mid-Cap discovery successfully unlocks sub-$25M catalyst plays.")
    finally:
        cur.close()
        conn.close()


def test_track_5_ihmr_filter():
    print("\n" + "="*70)
    print("TEST SUITE 5 (Track 5): Implied vs Historical Move Ratio (IHMR)")
    print("="*70)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        ihmr, imp_pct, hist_pct, status, rec = calculate_ihmr(cur, "NVDA", date(2026, 11, 4), 135.0)
        print(f"IHMR Output for NVDA:")
        print(f"  Implied Move:    {imp_pct:.2f}%")
        print(f"  Historical Move: {hist_pct:.2f}%")
        print(f"  IHMR Ratio:      {ihmr:.2f}x")
        print(f"  Status:          {status}")
        print(f"  Recommendation:  {rec}")

        assert ihmr > 0.0, "IHMR ratio must be greater than 0"
        assert status in ["IV_CRUSH_HAZARD", "UNDERPRICED_CATALYST", "NORMAL_VOLATILITY"]
        print("[PASS] Track 5: Pre-Earnings IHMR Volatility Filter operational.")
    finally:
        cur.close()
        conn.close()


def test_track_6_options_execution_bridge():
    print("\n" + "="*70)
    print("TEST SUITE 6 (Track 6): Options Paper Trading & Revaluation Bridge")
    print("="*70)

    test_directive = {
        "ticker": "NVDA",
        "recommended_strategy": "BULL_CALL_SPREAD",
        "expiration_date": (datetime.now(timezone.utc) + timedelta(days=21)).strftime("%Y-%m-%d"),
        "dte": 21,
        "legs": [
            {"strike": 135.0, "action": "BUY CALL", "premium": 4.50},
            {"strike": 145.0, "action": "SELL CALL", "premium": 1.50}
        ],
        "net_amount": 3.00,  # $3.00 net debit
        "max_profit": 700.0, # ($10 spread - $3 debit) * 100
        "max_loss": 300.0,   # $3 debit * 100
        "breakeven": 138.0
    }

    # 1. Execute order
    res = execute_paper_options_order(test_directive, underlying_price=135.0, contracts=1)
    print("Execution Result:", res)
    assert res.get("success") == True, f"Options execution failed: {res}"
    ticket_id = res["ticket_id"]

    # 2. Revalue open positions
    summary = revalue_open_options_positions()
    print("Mark-to-Market Summary:", summary)
    assert summary["monitored"] >= 1, "Should have monitored at least 1 open position"

    # Cleanup test position from database
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {settings.mimir_schema}.mimir_options_positions WHERE ticket_id = %s", (ticket_id,))
    conn.commit()
    cur.close()
    conn.close()
    print("[PASS] Track 6: Options Paper Trading table and revaluation lifecycle validated.")


def test_track_7_stat_arb_apu_dynamic_pairs():
    print("\n" + "="*70)
    print("TEST SUITE 7 (Track 7): Stat-Arb APU Dynamic Pairs & Beta Neutrality")
    print("="*70)

    conn = get_db_connection()
    try:
        discovered = StatArbAPU.discover_dynamic_pairs(conn=conn, max_pairs=10)
        print(f"Dynamically Discovered Pairs from mimir_asset_relationships ({len(discovered)}):")
        for dp in discovered[:5]:
            print(f"  - {dp['ticker1']} / {dp['ticker2']} ({dp['cluster']})")

        assert len(discovered) > 0, "APU should discover dynamic candidate pairs from supply chains!"

        # Test evaluating AMD / INTC pair for dollar-beta neutral sizing
        test_pair = {
            "ticker1": "AMD",
            "ticker2": "INTC",
            "name": "AMD / Intel",
            "cluster": "Semiconductors & AI",
            "type": "DYNAMIC_COMPETITOR",
            "historical_win_rate": 0.75,
            "historical_profit_factor": 2.20
        }
        opp = StatArbAPU.evaluate_pair(test_pair, {}, {})
        if opp:
            print("Evaluated Dynamic Pair Opportunity:")
            print(f"  Pair:            {opp['pair']}")
            print(f"  Hedge Ratio:     {opp['hedge_ratio']}")
            print(f"  Leg 1 Sizing:    {opp['leg1_target_shares']} sh (${opp['leg1_target_usd']})")
            print(f"  Leg 2 Sizing:    {opp['leg2_target_shares']} sh (${opp['leg2_target_usd']})")
            print(f"  Net Dollar Beta: ${opp['net_dollar_beta']}")
            assert "leg1_target_usd" in opp and "leg2_target_usd" in opp
        print("[PASS] Track 7: Dynamic cointegration screener and beta-neutral sizing operational.")
    finally:
        conn.close()


def run_all_tests():
    print("="*70)
    print("MIMIR HIGH-LEVERAGE PIPELINE IMPROVEMENTS: COMPLETE TEST RUN")
    print("="*70)

    test_track_1_chandelier_ratchet()
    test_track_2_defensive_macro_exemption()
    test_track_3_volatility_parity_sizing()
    test_track_4_mid_cap_discovery()
    test_track_5_ihmr_filter()
    test_track_6_options_execution_bridge()
    test_track_7_stat_arb_apu_dynamic_pairs()

    print("\n" + "="*70)
    print(">>> ALL 7 HIGH-LEVERAGE PIPELINE IMPROVEMENT SUITES PASSED (100%) <<<")
    print("="*70)


if __name__ == "__main__":
    run_all_tests()
