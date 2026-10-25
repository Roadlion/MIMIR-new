import os
import sys
from pathlib import Path

# Add version2 directory to path
v2_dir = Path(__file__).parent / "strategies&backtest" / "version2"
sys.path.insert(0, str(v2_dir))

from static_strategies import DualHedgeStrategy


def test_single_leg_tp():
    print("--- Test 1: Single-Leg TP Exit in INITIAL and SOLO States ---")
    strat = DualHedgeStrategy(zscore_entry=1.5, take_profit_pct=0.01, hedge_trigger_pct=0.005)
    
    # 1. Entry into INITIAL LONG
    actions = strat.tick(price=100.0, features={"ou_zscore": -2.0})
    assert actions == ["OPEN_LONG"], f"Expected OPEN_LONG, got {actions}"
    assert strat.state == "INITIAL"
    print("  [PASS] Initial LONG opened @ 100.0")

    # 2. Price rises to 101.5 (+1.5% PnL >= 1.0% take_profit_pct)
    actions = strat.tick(price=101.5, features={"ou_zscore": -1.0})
    assert actions == ["CLOSE_LONG"], f"Expected CLOSE_LONG on TP, got {actions}"
    assert strat.state == "FLAT"
    print("  [PASS] Initial LONG closed on TP @ 101.5 (+1.5%)")

    # 3. Test SOLO survivor single-leg TP exit
    strat.tick(price=100.0, features={"ou_zscore": -2.0})  # INITIAL LONG
    strat.tick(price=99.0, features={"ou_zscore": -2.5})   # HEDGED (opened SHORT @ 99.0)
    assert strat.state == "HEDGED"

    # Move price to cut losing SHORT and enter SOLO LONG
    # LONG @ 100.0 -> PnL @ 101.0 is +1.0% (win >= 0.5%)
    # SHORT @ 99.0 -> PnL @ 101.0 is -2.02% (loss <= -1.0% cut)
    actions = strat.tick(price=101.0, features={"ou_zscore": -0.5})
    assert actions == ["CLOSE_SHORT"], f"Expected CLOSE_SHORT, got {actions}"
    assert strat.state == "SOLO"
    assert strat.long_active is True
    print("  [PASS] Transitioned to SOLO survivor LONG")

    # Price moves to 102.5 (+2.5% PnL >= 1.0% TP) while z is still -0.2 (has not crossed 0.0)
    actions = strat.tick(price=102.5, features={"ou_zscore": -0.2})
    assert actions == ["CLOSE_LONG"], f"Expected CLOSE_LONG on SOLO TP, got {actions}"
    assert strat.state == "FLAT"
    print("  [PASS] SOLO survivor LONG closed on TP @ 102.5 (+2.5%)")


def test_single_leg_cut_loss():
    print("\n--- Test 3: Single-Leg Cut Loss in SOLO State ---")
    strat = DualHedgeStrategy(zscore_entry=1.5, cut_losing_pct=0.01, win_profit_pct=0.005)
    
    # Enter LONG @ 100, then HEDGE with SHORT @ 99, then cut SHORT @ 101 -> SOLO LONG @ 100
    strat.tick(price=100.0, features={"ou_zscore": -2.0})
    strat.tick(price=99.0, features={"ou_zscore": -2.5})
    strat.tick(price=101.0, features={"ou_zscore": -0.5})
    assert strat.state == "SOLO"

    # Now price drops to 98.8 (LONG @ 100 -> PnL = -1.2% <= -1.0% cut_losing_pct)
    actions = strat.tick(price=98.8, features={"ou_zscore": -0.5})
    assert actions == ["CLOSE_LONG"], f"Expected CLOSE_LONG on Cut Loss, got {actions}"
    assert strat.state == "FLAT"
    print("  [PASS] SOLO survivor LONG closed on Cut Loss @ 98.8 (-1.2% <= -1.0% cut_losing_pct)")


def test_state_persistence():
    print("\n--- Test 2: Strategy State Persistence & Restarts ---")
    test_file = str(Path(__file__).parent / "test_strategy_state.json")

    # 1. Create a strategy in SOLO state
    strat1 = DualHedgeStrategy(zscore_entry=1.5, take_profit_pct=0.01)
    strat1.tick(price=100.0, features={"ou_zscore": -2.0})
    strat1.tick(price=99.0, features={"ou_zscore": -2.5})
    strat1.tick(price=101.0, features={"ou_zscore": -0.5})  # Now in SOLO state with peak=101.0
    assert strat1.state == "SOLO"
    assert strat1.survivor_peak == 101.0

    # Save state
    strat1.save_state(test_file)
    print(f"  [PASS] Saved strategy state in SOLO mode (peak={strat1.survivor_peak})")

    # 2. Reload state in a new strategy instance
    strat2 = DualHedgeStrategy()
    loaded = strat2.load_state(test_file)
    assert loaded is True, "Failed to load state"
    assert strat2.state == "SOLO"
    assert strat2.long_active is True
    assert strat2.long_entry == 100.0
    assert strat2.survivor_peak == 101.0
    print("  [PASS] Successfully restored SOLO state across restart!")

    # Cleanup
    if os.path.exists(test_file):
        os.remove(test_file)


if __name__ == "__main__":
    test_single_leg_tp()
    test_single_leg_cut_loss()
    test_state_persistence()
    print("\n[SUCCESS] ALL TESTS PASSED SUCCESSFULLY!")
