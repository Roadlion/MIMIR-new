"""
Real-time Paper Trader for MetaTrader 5 (MT5).

Runs any StaticStrategy (dual_hedge, mean_reversion, ...) live on MT5
Demo / Paper Trading, optionally with an RLParameterTuner retuning the
strategy's parameters between trades based on TA (and, later,
sentiment) features.

To add a new static strategy: implement it in static_strategies.py and
register it in STRATEGY_REGISTRY. Nothing in this file changes.
To turn RL tuning on/off, flip USE_RL_TUNER below -- the strategy code
and this trading loop don't change either way.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import time
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from datetime import datetime

from data_fetching import add_features, fetch_historical_data
from static_strategies import STRATEGY_REGISTRY
from live_runner import LiveRunner
from rl_tuner import RLParameterTuner, HAS_RL

# ===========================================================================
# CONFIGURATION
# ===========================================================================
SYMBOL = "XAUUSD"
TIMEFRAME = mt5.TIMEFRAME_M1
CALC_TIMEFRAME = mt5.TIMEFRAME_M30
HISTORY_BARS = 7200
MIN_FEATURE_BARS = 150
CHECK_INTERVAL_SECONDS = 30
VOLUME = 0.01
MAGIC_NUMBER = 999888

# ---- Strategy selection: this is the ONLY switch you need to change strategy ----
STRATEGY_NAME = "dual_hedge"          # "dual_hedge" or "mean_reversion"
STRATEGY_KWARGS = {}                  # overrides for the strategy's __init__ defaults

# ---- RL tuning: independent of which strategy you picked above ----
USE_RL_TUNER = True
RL_FEATURE_COLUMNS = ["ou_zscore", "ewma_vol", "ou_theta", "rsi_14"]
# To add sentiment later: append its column name here (e.g. "sentiment_score")
# once data_fetching/add_features (or a separate sentiment pipeline) produces it.
RL_TOTAL_TIMESTEPS = 25_000
RL_MODEL_PATH = str(Path(__file__).parent / ".." / "models" / f"{STRATEGY_NAME}_rl_tuner.zip")
RETRAIN_EVERY_BARS = 10  # how many new calc-timeframe bars before RL retrains

EMERGENCY_SL_PCT = 0.03  # broker-level safety net; the strategy's own exits fire first


# ===========================================================================
# MT5 TRADE EXECUTION HELPERS
# ===========================================================================


def initialize_mt5(strategy_name: str):
    if not mt5.initialize():
        print(f"[ERROR] MT5 initialization failed: {mt5.last_error()}")
        sys.exit(1)

    account_info = mt5.account_info()
    if account_info is None:
        print(f"[ERROR] Failed to get account info: {mt5.last_error()}")
        sys.exit(1)

    print("=" * 60)
    print(" 🚀 MIMIR REALTIME MT5 PAPER TRADER")
    print("=" * 60)
    print(f" Account Login:   {account_info.login}")
    print(f" Broker Server:  {account_info.server}")
    print(f" Balance:        ${account_info.balance:,.2f}")
    print(f" Equity:         ${account_info.equity:,.2f}")
    print(f" Symbol:         {SYMBOL}")
    print(f" Strategy:       {strategy_name}")
    print(f" RL tuning:      {'ON' if USE_RL_TUNER else 'OFF'}")
    print("=" * 60)

    if not mt5.symbol_select(SYMBOL, True):
        print(f"[ERROR] Symbol '{SYMBOL}' not available in Market Watch!")
        sys.exit(1)


def get_current_position(symbol, magic=MAGIC_NUMBER):
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return []
    return [p for p in positions if p.magic == magic]


def send_order(symbol, order_type, volume, sl_dist=0.0, tp_dist=0.0, magic=MAGIC_NUMBER, comment=""):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"[ERROR] Could not fetch tick for {symbol}")
        return None

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    sl_price = (price - sl_dist if order_type == mt5.ORDER_TYPE_BUY else price + sl_dist) if sl_dist > 0 else 0.0
    tp_price = (price + tp_dist if order_type == mt5.ORDER_TYPE_BUY else price - tp_dist) if tp_dist > 0 else 0.0

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "sl": float(sl_price),
        "tp": float(tp_price),
        "deviation": 20,
        "magic": magic,
        "comment": comment or "MIMIR",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else mt5.last_error()
        print(f"[ORDER REJECTED] {order_type} on {symbol}: {err}")
    else:
        side_str = "BUY (LONG)" if order_type == mt5.ORDER_TYPE_BUY else "SELL (SHORT)"
        print(f"[ORDER EXECUTED ✅] {side_str} {volume} lot @ {price:.2f} | Ticket #{result.order}")
    return result


def close_position(position):
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return None

    close_type = mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position.type == mt5.ORDER_TYPE_BUY else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": position.symbol,
        "volume": float(position.volume),
        "type": close_type,
        "position": position.ticket,
        "price": price,
        "deviation": 20,
        "magic": position.magic,
        "comment": "MIMIR Close Signal",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"[POSITION CLOSED ✅] Ticket #{position.ticket} closed @ {price:.2f}")
    else:
        err = result.comment if result else mt5.last_error()
        print(f"[CLOSE FAILED ❌] Ticket #{position.ticket}: {err}")
    return result


def execute_actions(actions, current_price, strategy, time_str):
    """Translate LiveRunner actions into MT5 orders. Generic across all
    strategies since actions are always one of the four canonical strings."""
    strategy_name = getattr(strategy, "name", STRATEGY_NAME)
    tp_pct = getattr(strategy, "take_profit_pct", 0.0)
    tp_dist = current_price * tp_pct if tp_pct > 0 else 0.0

    for act in actions:
        if act == "OPEN_LONG":
            print(f"[{time_str}] 🚀 OPEN LONG @ {current_price:,.2f}")
            send_order(SYMBOL, mt5.ORDER_TYPE_BUY, VOLUME,
                       sl_dist=current_price * EMERGENCY_SL_PCT, tp_dist=tp_dist,
                       comment=f"MIMIR {strategy_name}")

        elif act == "OPEN_SHORT":
            print(f"[{time_str}] 🔻 OPEN SHORT @ {current_price:,.2f}")
            send_order(SYMBOL, mt5.ORDER_TYPE_SELL, VOLUME,
                       sl_dist=current_price * EMERGENCY_SL_PCT, tp_dist=tp_dist,
                       comment=f"MIMIR {strategy_name}")

        elif act == "CLOSE_LONG":
            for p in get_current_position(SYMBOL, magic=MAGIC_NUMBER):
                if p.type == mt5.ORDER_TYPE_BUY:
                    pnl_pct = (current_price - p.price_open) / p.price_open
                    tag = "✅ WIN" if pnl_pct > 0 else "❌ LOSS"
                    print(f"[{time_str}] {tag} CLOSE LONG (PnL: {pnl_pct:+.2%})")
                    close_position(p)

        elif act == "CLOSE_SHORT":
            for p in get_current_position(SYMBOL, magic=MAGIC_NUMBER):
                if p.type == mt5.ORDER_TYPE_SELL:
                    pnl_pct = (p.price_open - current_price) / p.price_open
                    tag = "✅ WIN" if pnl_pct > 0 else "❌ LOSS"
                    print(f"[{time_str}] {tag} CLOSE SHORT (PnL: {pnl_pct:+.2%})")
                    close_position(p)


def sync_positions_with_mt5(runner: LiveRunner):
    """Detect actual MT5 positions and resync the strategy if a leg was
    closed externally (e.g. the broker's emergency stop-loss)."""
    positions = get_current_position(SYMBOL, magic=MAGIC_NUMBER)
    mt5_has_long = any(p.type == mt5.ORDER_TYPE_BUY for p in positions)
    mt5_has_short = any(p.type == mt5.ORDER_TYPE_SELL for p in positions)

    strategy = runner.strategy
    resync_needed = (
        (getattr(strategy, "long_active", strategy.net_position > 0) and not mt5_has_long) or
        (getattr(strategy, "short_active", strategy.net_position < 0) and not mt5_has_short)
    )

    if resync_needed:
        long_entry = next((p.price_open for p in positions if p.type == mt5.ORDER_TYPE_BUY), 0.0)
        short_entry = next((p.price_open for p in positions if p.type == mt5.ORDER_TYPE_SELL), 0.0)
        print("[SYNC] ⚠ Position(s) closed externally — resyncing strategy state.")
        runner.sync_from_broker(
            mt5_has_long, long_entry, mt5_has_short, short_entry,
            state_hint=strategy.state, survivor_peak=getattr(strategy, "survivor_peak", 0.0)
        )


# ===========================================================================
# MAIN REALTIME LOOP
# ===========================================================================


def run_realtime_trader():
    strategy_cls = STRATEGY_REGISTRY[STRATEGY_NAME]
    strategy = strategy_cls(**STRATEGY_KWARGS)

    STATE_FILE_PATH = str(Path(__file__).parent / f"{STRATEGY_NAME}_state.json")
    if hasattr(strategy, "load_state"):
        if strategy.load_state(STATE_FILE_PATH):
            print(f"[STATE] Loaded saved strategy state from {STATE_FILE_PATH}")
            print(f"[STATE] Restored: {strategy.status_line()}")
        else:
            print("[STATE] No previous saved state found. Starting fresh.")

    tuner = None
    if USE_RL_TUNER:
        if not HAS_RL:
            print("[WARN] gymnasium/stable_baselines3 not installed — running WITHOUT RL tuning.")
        else:
            tuner = RLParameterTuner(
                strategy_cls, STRATEGY_KWARGS, RL_FEATURE_COLUMNS,
                model_path=RL_MODEL_PATH, total_timesteps=RL_TOTAL_TIMESTEPS,
            )

    runner = LiveRunner(strategy, tuner)

    initialize_mt5(STRATEGY_NAME)
    print("\n" + "=" * 60)
    print(f" 🔄 LIVE {STRATEGY_NAME.upper()}" + (" + RL PARAMETER TUNING" if tuner else ""))
    print("=" * 60)
    print(" • Strategy manages ALL entries and exits")
    print(" • MT5 SL is emergency-only — the strategy's own logic exits first")
    if tuner:
        print(" • RL retunes parameters, but only while the strategy is FLAT")
    print("=" * 60)

    bars_since_retrain = 0
    last_calc_time = None

    existing = get_current_position(SYMBOL, magic=MAGIC_NUMBER)
    if existing:
        ex_longs = [p for p in existing if p.type == mt5.ORDER_TYPE_BUY]
        ex_shorts = [p for p in existing if p.type == mt5.ORDER_TYPE_SELL]
        strategy.sync_from_broker(
            bool(ex_longs), ex_longs[0].price_open if ex_longs else 0.0,
            bool(ex_shorts), ex_shorts[0].price_open if ex_shorts else 0.0,
            state_hint=strategy.state, survivor_peak=getattr(strategy, "survivor_peak", 0.0)
        )
    else:
        print("[SYNC] No existing positions — starting FLAT.")
        if strategy.state != "FLAT":
            strategy.sync_from_broker(False, 0.0, False, 0.0)

    if tuner:
        print(f"\n[RL TRAIN] Fetching {HISTORY_BARS} historical bars for PPO pre-training...")
        rates = mt5.copy_rates_from_pos(SYMBOL, CALC_TIMEFRAME, 1, HISTORY_BARS)
        if rates is not None and len(rates) >= MIN_FEATURE_BARS:
            hist_df = pd.DataFrame(rates)
            hist_df["time"] = pd.to_datetime(hist_df["time"], unit="s")
            hist_df["symbol"] = SYMBOL
            hist_df.rename(columns={"tick_volume": "volume"}, inplace=True)
            hist_df = add_features(hist_df).replace([np.inf, -np.inf], np.nan)
            hist_df = hist_df.dropna(subset=RL_FEATURE_COLUMNS).reset_index(drop=True)
            if len(hist_df) >= MIN_FEATURE_BARS:
                tuner.train(hist_df)
                print("[RL TRAIN] Pre-training complete.")
            else:
                print("[RL TRAIN] Not enough valid feature bars — RL will use default params until retrained.")
        else:
            print("[RL TRAIN] Not enough historical data — RL will use default params until retrained.")

    try:
        while True:
            rates = mt5.copy_rates_from_pos(SYMBOL, CALC_TIMEFRAME, 1, HISTORY_BARS)
            if rates is None or len(rates) < MIN_FEATURE_BARS:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for bar data...")
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            df_calc = pd.DataFrame(rates)
            df_calc["time"] = pd.to_datetime(df_calc["time"], unit="s")
            df_calc["symbol"] = SYMBOL
            df_calc.rename(columns={"tick_volume": "volume"}, inplace=True)
            df_calc.dropna(inplace=True)

            tick = mt5.symbol_info_tick(SYMBOL)
            if tick is None:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            current_price = (tick.ask + tick.bid) / 2.0
            live_time = datetime.now()

            live_bar = pd.DataFrame([{
                "time": live_time, "open": current_price, "high": current_price,
                "low": current_price, "close": current_price, "volume": 1.0, "symbol": SYMBOL,
            }])
            df_eval = pd.concat([df_calc, live_bar], ignore_index=True)

            df_featured = add_features(df_eval).replace([np.inf, -np.inf], np.nan)
            df_featured = df_featured.dropna(subset=RL_FEATURE_COLUMNS).reset_index(drop=True)
            if len(df_featured) < MIN_FEATURE_BARS:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            latest_row = df_featured.iloc[-1]
            features = {c: latest_row.get(c, 0.0) for c in RL_FEATURE_COLUMNS}

            sync_positions_with_mt5(runner)

            if tuner:
                current_calc_time = df_calc["time"].iloc[-1]
                if last_calc_time is not None and current_calc_time != last_calc_time:
                    bars_since_retrain += 1
                    if bars_since_retrain >= RETRAIN_EVERY_BARS:
                        print(f"\n[RL RETRAIN] Re-training on {len(df_featured)} bars...")
                        tuner.train(df_featured)
                        bars_since_retrain = 0
                last_calc_time = current_calc_time

            actions = runner.on_tick(current_price, features)

            time_str = live_time.strftime("%H:%M:%S")
            execute_actions(actions, current_price, strategy, time_str)

            if hasattr(strategy, "save_state"):
                strategy.save_state(STATE_FILE_PATH)

            z = features.get("ou_zscore", float("nan"))
            z_str = f"{z:+.2f}" if z == z else "N/A"
            print(f"[{time_str}] Price: {current_price:,.2f} | Z: {z_str:>6} | {runner.status_line(current_price)}")

            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[STOPPED] Realtime Paper Trader shut down by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    run_realtime_trader()
