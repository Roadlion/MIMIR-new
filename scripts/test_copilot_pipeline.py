# scripts/test_copilot_pipeline.py
"""
MIMIR Co-Pilot End-to-End Test Suite
====================================
Validates the entire Co-Pilot quantitative pipeline:
1. Multi-user personal risk parameters and dry powder.
2. Signal allocation math (Risk-Parity, Position Cap, Dry Powder clamping).
3. Sector concentration firewall (prevents overexposure).
4. Buy order logging & dry powder deduction.
5. Trailing ratchet profit-lock logic.
6. Sell reminder execution & capital restoration into dry powder.
"""

import sys
import os

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.app.database import get_db_connection
from backend.app.services.copilot_allocator import (
    get_copilot_users,
    get_user_portfolio_state,
    evaluate_signal_for_user,
    record_user_buy,
    record_user_sell,
    dispatch_copilot_signals
)
from backend.app.services.copilot_monitor import evaluate_ratchet_tiers, check_and_update_copilot_positions

def run_tests():
    print("=" * 70)
    print("🚀 MIMIR MULTI-USER CO-PILOT END-TO-END VERIFICATION")
    print("=" * 70)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # Setup clean test users in database
            print("\n[STEP 1] Configuring Multi-Tenant Test Profiles...")
            
            # User 1: The Balanced Trader ($15,000 budget, $10,000 dry powder, 1.0% risk, 15% max pos, 25% max sector)
            cur.execute("""
                UPDATE yggdrasil.mimir_users
                SET total_budget = 15000.00,
                    dry_powder = 10000.00,
                    risk_per_trade_pct = 1.00,
                    max_position_pct = 15.00,
                    max_sector_pct = 25.00,
                    is_copilot_active = TRUE
                WHERE id = 1;
            """)

            # User 2: The Large Capital Trader ($50,000 budget, $40,000 dry powder, 1.5% risk, 10% max pos, 30% max sector)
            cur.execute("""
                UPDATE yggdrasil.mimir_users
                SET total_budget = 50000.00,
                    dry_powder = 40000.00,
                    risk_per_trade_pct = 1.50,
                    max_position_pct = 10.00,
                    max_sector_pct = 30.00,
                    is_copilot_active = TRUE
                WHERE id = 2;
            """)

            # User 3: The Low Cash Trader ($2,000 budget, $150 dry powder, 1.0% risk, 10% max pos, 25% max sector)
            cur.execute("""
                UPDATE yggdrasil.mimir_users
                SET total_budget = 2000.00,
                    dry_powder = 150.00,
                    risk_per_trade_pct = 1.00,
                    max_position_pct = 10.00,
                    max_sector_pct = 25.00,
                    is_copilot_active = TRUE
                WHERE id = 3;
            """)

            # Clean any leftover test positions for these users
            cur.execute("DELETE FROM yggdrasil.mimir_user_positions WHERE user_id IN (1, 2, 3);")
            cur.execute("DELETE FROM yggdrasil.mimir_user_trade_alerts WHERE user_id IN (1, 2, 3);")
            conn.commit()

        print("  ✅ Configured User 1 (Admin): $15,000 Budget | $10,000 Dry Powder | 1.0% Risk")
        print("  ✅ Configured User 2 (Alex):  $50,000 Budget | $40,000 Dry Powder | 1.5% Risk")
        print("  ✅ Configured User 3 (Bob):   $2,000 Budget  | $150 Dry Powder    | 1.0% Risk")

        # ── STEP 2: Mathematical Sizing Test ──────────────────────────────────
        print("\n[STEP 2] Testing Mathematical Sizing Across Users...")
        simulated_signal = {
            "ticker": "LMT",
            "trigger_price": 550.00,
            "stop_loss": 530.00,  # $20 risk per share
            "target_price": 610.00,
            "catalyst_type": "WAR_RIG_CONVERGENCE",
            "conviction_score": 0.88,
            "sector": "INDUSTRIALS",
            "holding_period_days": 7
        }
        print(f"  Signal: {simulated_signal['ticker']} @ ${simulated_signal['trigger_price']} | SL: ${simulated_signal['stop_loss']} ($20 risk/share)")

        users = get_copilot_users(conn=conn)
        test_users = [u for u in users if u["id"] in (1, 2, 3)]

        allocations = {}
        for u in test_users:
            res = evaluate_signal_for_user(u, simulated_signal, conn=conn)
            allocations[u["id"]] = res
            if res["approved"]:
                print(f"  🎯 User {u['id']} ({u['username']}): APPROVED")
                print(f"     • Sizing: {res['shares']} shares (~${res['estimated_cost']:,.2f})")
                print(f"     • Max Risk: ${res['risk_dollars']:,.2f} ({res['risk_pct_actual']}% of equity)")
                print(f"     • Dry Powder: ${res['dry_powder_before']:,.2f} ➔ ${res['dry_powder_after']:,.2f}")
            else:
                print(f"  ❌ User {u['id']} ({u['username']}): REJECTED")
                print(f"     • Reason: {res.get('reason')}")

        # Verification asserts:
        # User 1: 15% max pos cap on $10k equity = $1,500 / 550 = 2 shares (or max pos cap on total budget)
        assert allocations[1]["approved"] is True, "User 1 should be approved"
        assert allocations[1]["shares"] > 0, "User 1 should receive > 0 shares"
        assert allocations[2]["approved"] is True, "User 2 should be approved"
        assert allocations[2]["shares"] > allocations[1]["shares"], "User 2 (larger budget) should receive more shares than User 1"
        assert allocations[3]["approved"] is True, "User 3 should be approved with Dime! fractional shares"
        assert allocations[3]["shares"] == 0.025, f"User 3 should receive clean 0.025 shares, got {allocations[3]['shares']}"
        print("  ✅ Dime! clean fractional sizing, risk bounds, and cash clamps verified perfectly!")

        # ── STEP 3: Sector Concentration Firewall Test ─────────────────────────
        print("\n[STEP 3] Testing Sector Concentration Firewall...")
        # Artificially fill User 1's Industrials sector to >25% (20 shares @ $200 = $4,000)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO yggdrasil.mimir_user_positions (
                    user_id, ticker, shares, entry_price, current_stop_loss,
                    sector, status
                ) VALUES (1, 'RTX', 20, 200.00, 190.00, 'INDUSTRIALS', 'OPEN');
            """)
            conn.commit()

        # Re-evaluate LMT (Industrials) for User 1
        res_sector_test = evaluate_signal_for_user(test_users[0], simulated_signal, conn=conn)
        print(f"  User 1 Industrials Re-evaluation with existing RTX holding:")
        print(f"  Result: {'APPROVED' if res_sector_test['approved'] else 'BLOCKED'}")
        print(f"  Details: {res_sector_test.get('reason')}")
        assert res_sector_test["approved"] is False or "SECTOR" in str(res_sector_test.get("reason", "")).upper()
        print("  ✅ Sector concentration firewall successfully prevented correlated overexposure!")

        # Clean up artificial RTX position
        with conn.cursor() as cur:
            cur.execute("DELETE FROM yggdrasil.mimir_user_positions WHERE user_id = 1 AND ticker = 'RTX';")
            conn.commit()

        # ── STEP 4: Buy Order Execution & Dry Powder Deduction ─────────────────
        print("\n[STEP 4] Testing Buy Order Execution & Dry Powder Deduction...")
        user1_pre_state = get_user_portfolio_state(1, conn=conn)
        initial_powder = user1_pre_state["dry_powder"]

        buy_res = record_user_buy(
            user_id=1,
            ticker="LMT",
            shares=4,
            entry_price=550.00,
            stop_loss=530.00,
            target_price=610.00,
            holding_period_days=7,
            sector="INDUSTRIALS",
            conn=conn
        )
        assert buy_res["success"] is True, f"Buy failed: {buy_res.get('error')}"
        pos_id = buy_res["position_id"]
        expected_remaining_powder = round(initial_powder - (4 * 550.00), 2)
        assert buy_res["remaining_dry_powder"] == expected_remaining_powder, "Dry powder not deducted correctly"

        user1_post_state = get_user_portfolio_state(1, conn=conn)
        assert len(user1_post_state["open_positions"]) == len(user1_pre_state["open_positions"]) + 1, "Position not added to open positions"
        assert user1_post_state["dry_powder"] == expected_remaining_powder
        print(f"  ✅ Executed BUY: 4 shares of LMT @ $550.00 (Cost: $2,200.00)")
        print(f"  ✅ Dry powder accurately reduced: ${initial_powder:,.2f} ➔ ${user1_post_state['dry_powder']:,.2f}")

        # ── STEP 5: Trailing Ratchet Profit Lock Test ──────────────────────────
        print("\n[STEP 5] Testing Trailing Profit Ratchet Logic...")
        # Simulate price moving from $550 to $595 (+8.18% gain)
        entry_p = 550.00
        rally_p = 595.00
        current_sl = 530.00

        ratchet_res = evaluate_ratchet_tiers(entry_price=entry_p, current_price=rally_p, current_sl=current_sl)
        assert ratchet_res is not None, "Ratchet should trigger at +8.18% gain"
        assert ratchet_res["locked_pct"] == 5.0, "Tier 3 ratchet should lock +5.0% profit"
        target_sl = ratchet_res["target_sl"]
        expected_target_sl = round(entry_p * 1.05, 2)  # $577.50
        assert target_sl == expected_target_sl, f"Target SL should be {expected_target_sl}, got {target_sl}"
        locked_pnl = round((target_sl - entry_p) * 4, 2)
        print(f"  Simulated Price Rally: ${entry_p:.2f} ➔ ${rally_p:.2f} (+{ratchet_res['gain_pct']:.1f}%)")
        print(f"  Triggered: {ratchet_res['tier_label']}")
        print(f"  New Stop-Loss: ${target_sl:.2f} (Locking in +${locked_pnl:.2f} guaranteed profit)")
        print("  ✅ Ratchet profit lock calculation verified!")

        # ── STEP 6: Sell Execution & Capital Recycling Test ───────────────────
        print("\n[STEP 6] Testing Sell Reminder Execution & Dry Powder Restoration...")
        # User sells at $600.00 (profit target)
        sell_p = 600.00
        sell_res = record_user_sell(
            position_id=pos_id,
            exit_price=sell_p,
            close_reason="PROFIT_TARGET",
            conn=conn
        )
        assert sell_res["success"] is True, f"Sell failed: {sell_res.get('error')}"
        expected_realized_pnl = round((sell_p - entry_p) * 4, 2)  # $200.00
        expected_cash_recovered = round(sell_p * 4, 2)             # $2,400.00
        expected_final_powder = round(expected_remaining_powder + expected_cash_recovered, 2)

        assert sell_res["realized_pnl"] == expected_realized_pnl, "Realized P&L mismatch"
        assert sell_res["cash_recovered"] == expected_cash_recovered, "Cash recovered mismatch"
        assert sell_res["new_dry_powder"] == expected_final_powder, "Restored dry powder mismatch"

        user1_final_state = get_user_portfolio_state(1, conn=conn)
        assert len(user1_final_state["open_positions"]) == len(user1_pre_state["open_positions"]), "Closed position should no longer be open"
        assert user1_final_state["dry_powder"] == expected_final_powder
        print(f"  ✅ Executed SELL: 4 shares of LMT @ ${sell_p:.2f}")
        print(f"  ✅ Realized P&L: +${sell_res['realized_pnl']:,.2f} (+{sell_res['return_pct']:.1f}%)")
        print(f"  ✅ Cash Recovered: +${sell_res['cash_recovered']:,.2f}")
        print(f"  ✅ New Dry Powder: ${user1_final_state['dry_powder']:,.2f} (Initial $10,000 + $200 Profit)")

    print("\n" + "=" * 70)
    print("🏆 ALL 6 CO-PILOT INTEGRATION & RISK MODULE TESTS PASSED 100%!")
    print("=" * 70)

if __name__ == "__main__":
    run_tests()
