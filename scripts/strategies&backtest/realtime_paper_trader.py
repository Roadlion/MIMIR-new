"""
Real-time Paper Trader for MetaTrader 5 (MT5).

Runs your tuned strategy (DualHedgeSelectionStrategy or MeanReversionMLStrategy)
live on MT5 Demo / Paper Trading account.

Execution Flow:
1. Connects to active MT5 terminal (Demo account).
2. On every new bar / sleep interval:
   - Fetches historical bars from MT5.
   - Computes features (add_features).
   - Generates latest strategy signal (Long +1, Short -1, Flat 0).
3. Executes paper/demo orders via MT5 trade API with automatic Stop Loss & Take Profit.
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
from strategies import (
    DualHedgeSelectionStrategy,
    MeanReversionMLStrategy,
    RLAdaptiveStrategy,
)

# ===========================================================================
# CONFIGURATION
# ===========================================================================
SYMBOL = "BTCUSD"  # MT5 symbol to trade (e.g. BTCUSD, EURUSD, AAPL.US)
# Data fetch timeframe (e.g., mt5.TIMEFRAME_M1 for 1‑minute bars)
TIMEFRAME = mt5.TIMEFRAME_M1  # Minute‑level data
# Calculation timeframe for features (e.g., hourly aggregation)
CALC_TIMEFRAME = mt5.TIMEFRAME_M30  # Aggregated period used for feature calculation
HISTORY_BARS = 7200  # Closed calculation-timeframe bars to retain
MIN_FEATURE_BARS = 150  # Enough history after the 100-bar OU warm-up
CHECK_INTERVAL_SECONDS = 30  # Poll MT5 every N seconds for new bars
VOLUME = 0.01  # Trade size (lots)
MAGIC_NUMBER = 999888  # Order identifier for this strategy

# Choose Strategy: "dual_hedge", "mean_reversion_ml", or "rl_adaptive"
STRATEGY_TYPE = "rl_adaptive"

if STRATEGY_TYPE == "dual_hedge":
    STRATEGY = DualHedgeSelectionStrategy(
        ou_lookback=100,
        zscore_entry=1,
        hedge_trigger_loss_pct=0.025,
        cut_losing_leg_pct=0.025,
        winning_leg_profit_pct=0.05,
        trailing_stop_pct=0.01,
        take_profit_pct=0.05,
    )
elif STRATEGY_TYPE == "rl_adaptive":
    STRATEGY = RLAdaptiveStrategy(
        total_timesteps=25_000,
        train_ratio=0.50,
    )
else:
    STRATEGY = MeanReversionMLStrategy(
        ou_lookback=100,
        zscore_entry=1,
        zscore_exit=0.0,
        zscore_flip=2.5,
        ml_confidence=0.80,
        vol_stop_mult=3.0,
        max_risk_pct=0.1,
        train_ratio=0.25,
        retrain_interval=200,
    )


# ===========================================================================
# MT5 TRADE EXECUTION HELPERS
# ===========================================================================


def initialize_mt5():
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
    print(f" Timeframe:      H1")
    print(f" Strategy:       {STRATEGY.name}")
    print("=" * 60)

    if not mt5.symbol_select(SYMBOL, True):
        print(f"[ERROR] Symbol '{SYMBOL}' not available in Market Watch!")
        sys.exit(1)


def get_current_position(symbol, magic=MAGIC_NUMBER):
    """Check current open positions for this symbol and magic number."""
    positions = mt5.positions_get(symbol=symbol)
    if positions is None:
        return []
    return [p for p in positions if p.magic == magic]


def send_order(
    symbol, order_type, volume, sl_dist=0.0, tp_dist=0.0, magic=MAGIC_NUMBER
):
    """Sends a Buy or Sell order to MT5."""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"[ERROR] Could not fetch tick for {symbol}")
        return None

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

    sl_price = 0.0
    tp_price = 0.0

    if sl_dist > 0:
        sl_price = (
            price - sl_dist if order_type == mt5.ORDER_TYPE_BUY else price + sl_dist
        )
    if tp_dist > 0:
        tp_price = (
            price + tp_dist if order_type == mt5.ORDER_TYPE_BUY else price - tp_dist
        )

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
        "comment": f"MIMIR {STRATEGY.name}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        err = result.comment if result else mt5.last_error()
        print(f"[ORDER REJECTED] {order_type} on {symbol}: {err}")
    else:
        side_str = "BUY (LONG)" if order_type == mt5.ORDER_TYPE_BUY else "SELL (SHORT)"
        print(
            f"[ORDER EXECUTED ✅] {side_str} {volume} lot @ {price:.2f} | Ticket #{result.order}"
        )
    return result


def close_position(position):
    """Closes an open position."""
    tick = mt5.symbol_info_tick(position.symbol)
    if tick is None:
        return None

    close_type = (
        mt5.ORDER_TYPE_SELL
        if position.type == mt5.ORDER_TYPE_BUY
        else mt5.ORDER_TYPE_BUY
    )
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


# ===========================================================================
# RL ADAPTIVE: LIVE INFERENCE HELPERS
# ===========================================================================

# How many new H1 bars before RL agent re-trains on fresh data
RETRAIN_EVERY_BARS = 10
RL_FEATURE_COLUMNS = ("close", "ou_zscore", "ou_theta", "ewma_vol", "rsi_14")

# Wide emergency stop-loss (broker-level safety net).
# The DualHedge state machine handles normal exits well before this.
EMERGENCY_SL_PCT = 0.03  # 3%


def _prepare_feature_data(df):
    """Calculate and validate the finite features consumed by PPO."""
    featured = add_features(df)
    featured = featured.replace([np.inf, -np.inf], np.nan)
    return featured.dropna(subset=RL_FEATURE_COLUMNS).reset_index(drop=True)


def _rl_train_on_history(strategy, n_bars=HISTORY_BARS):
    """Fetch historical bars from MT5 and train the RL agent before going live."""
    print(f"\n[RL TRAIN] Fetching {n_bars} historical bars for PPO pre-training...")
    # Position 1 excludes the still-forming calculation-timeframe candle.
    rates = mt5.copy_rates_from_pos(SYMBOL, CALC_TIMEFRAME, 1, n_bars)
    if rates is None or len(rates) < 200:
        print("[RL TRAIN] Not enough historical data for training!")
        return None

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df["symbol"] = SYMBOL
    df.rename(columns={"tick_volume": "volume"}, inplace=True)
    df = _prepare_feature_data(df)
    if len(df) < MIN_FEATURE_BARS:
        print(f"[RL TRAIN] Need {MIN_FEATURE_BARS} valid feature bars; got {len(df)}.")
        return None

    # generate_signals internally trains PPO on train_ratio of data then
    # walks forward — we call it once so the model is ready.
    signals = strategy.generate_signals(df)
    print(f"[RL TRAIN] Pre-training complete. Model ready for live inference.")
    return df


def _rl_get_params(strategy, df_featured):
    """Use trained PPO model to predict optimal DualHedge parameters
    for the current market regime observation.

    Builds a single observation vector matching TradingParamEnv's
    observation space and decodes the 6-D action into concrete
    parameter values.

    Returns:
        dict of parameter values, or empty dict to use defaults.
    """
    from strategies import HAS_RL

    if not HAS_RL or strategy._model is None:
        return {}

    # Build observation vector: [z, vol, theta, rsi, slope, dd]
    row = df_featured.iloc[-1]
    z = row.get("ou_zscore", 0.0)
    vol = row.get("ewma_vol", 0.01)
    theta = row.get("ou_theta", 0.01)
    rsi = (row.get("rsi_14", 50.0) - 50.0) / 50.0

    # 20-bar trend slope
    lookback_idx = max(0, len(df_featured) - 21)
    p_curr = row["close"]
    p_prev = df_featured["close"].iloc[lookback_idx]
    slope = (p_curr - p_prev) / p_prev if p_prev > 0 else 0.0

    # Drawdown not tracked in live — use 0.0
    dd = 0.0

    obs = np.array([z, vol, theta, rsi, slope, dd], dtype=np.float32)
    obs = np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)

    action, _ = strategy._model.predict(obs, deterministic=True)

    return {
        "zscore_entry": float(np.interp(action[0], [-1, 1], [1.0, 2.5])),
        "hedge_trigger_pct": float(np.interp(action[1], [-1, 1], [0.002, 0.012])),
        "cut_losing_pct": float(np.interp(action[2], [-1, 1], [0.005, 0.020])),
        "win_profit_pct": float(np.interp(action[3], [-1, 1], [0.003, 0.015])),
        "trail_stop_pct": float(np.interp(action[4], [-1, 1], [0.004, 0.015])),
        "take_profit_pct": float(np.interp(action[5], [-1, 1], [0.008, 0.030])),
    }


def _snapshot_params(self):
    self._active = {
        "hedge_trigger_pct": self.hedge_trigger_pct,
        "cut_losing_pct": self.cut_losing_pct,
        "win_profit_pct": self.win_profit_pct,
        "trail_stop_pct": self.trail_stop_pct,
        "take_profit_pct": self.take_profit_pct,
    }


def _p(self, key):
    """Read a param — locked value if in a trade, live value if flat."""
    return self._active[key] if self._active else getattr(self, key)


# ===========================================================================
# LIVE DUAL-HEDGE STATE MACHINE
# ===========================================================================


class LiveDualHedgeManager:
    """Persistent DualHedge state machine for real-time MT5 execution.

    Maintains the 4-state finite state machine across ticks and returns
    concrete MT5 actions (OPEN_LONG, CLOSE_SHORT, etc.) on each state
    transition.  Unlike the backtest environment which replays from
    scratch, this class is created ONCE and lives for the entire trading
    session.

    States
    ------
    FLAT → INITIAL_TRADE → HEDGED_DUAL → SOLO_SURVIVOR
    """

    FLAT = "FLAT"
    INITIAL = "INITIAL_TRADE"
    HEDGED = "HEDGED_DUAL"
    SOLO = "SOLO_SURVIVOR"

    def __init__(self):
        self.state = self.FLAT
        self.long_active = False
        self.long_entry = 0.0
        self.short_active = False
        self.short_entry = 0.0
        self.survivor_peak = 0.0

        # Default parameters — overridden by RL each tick
        self.zscore_entry = 1.5
        self.hedge_trigger_pct = 0.005
        self.cut_losing_pct = 0.010
        self.win_profit_pct = 0.005
        self.trail_stop_pct = 0.008
        self.take_profit_pct = 0.015

        # Frozen snapshot used by tick() while a trade is open
        self._active = None

    # ---- helpers ----

    @property
    def net_position(self) -> float:
        return (1.0 if self.long_active else 0.0) - (1.0 if self.short_active else 0.0)

    def update_params(self, params: dict):
        """Update strategy parameters from RL model output."""
        for key, val in params.items():
            if hasattr(self, key):
                setattr(self, key, val)

    def sync_from_mt5(
        self, has_long: bool, long_entry: float, has_short: bool, short_entry: float
    ):
        """Sync internal state from existing MT5 positions on startup or
        after detecting an external close (e.g. broker emergency SL)."""
        self.long_active = has_long
        self.long_entry = long_entry if has_long else 0.0
        self.short_active = has_short
        self.short_entry = short_entry if has_short else 0.0

        if has_long and has_short:
            self.state = self.HEDGED
        elif has_long or has_short:
            # Could be INITIAL or SOLO — we can't tell, assume INITIAL
            # which is safer (it will transition naturally).
            self.state = self.INITIAL
        else:
            self.state = self.FLAT
            self.survivor_peak = 0.0

        print(
            f"[SYNC] State → {self.state} | "
            f"Long={'ON @'+f'{long_entry:.2f}' if has_long else 'off'} | "
            f"Short={'ON @'+f'{short_entry:.2f}' if has_short else 'off'}"
        )

    def status_line(self, price: float = 0.0) -> str:
        """Human-readable status string with live PnL."""
        parts = [f"State={self.state}"]
        if self.long_active and self.long_entry > 0 and price > 0:
            pnl = (price - self.long_entry) / self.long_entry
            parts.append(f"LONG@{self.long_entry:.2f}({pnl:+.2%})")
        elif self.long_active:
            parts.append(f"LONG@{self.long_entry:.2f}")
        if self.short_active and self.short_entry > 0 and price > 0:
            pnl = (self.short_entry - price) / self.short_entry
            parts.append(f"SHORT@{self.short_entry:.2f}({pnl:+.2%})")
        elif self.short_active:
            parts.append(f"SHORT@{self.short_entry:.2f}")
        if self.state == self.SOLO and self.survivor_peak > 0:
            parts.append(f"Peak={self.survivor_peak:.2f}")

        # NEW: show live parameters (esp. useful when RL is retuning them each tick)
        parts.append(
            f"Params[z={self.zscore_entry:.2f} "
            f"hedge={self.hedge_trigger_pct:.3%} "
            f"cut={self.cut_losing_pct:.3%} "
            f"win={self.win_profit_pct:.3%} "
            f"trail={self.trail_stop_pct:.3%} "
        )

        return " | ".join(parts)

    # ---- core state machine ----

    def tick(self, price: float, z_score: float) -> list:
        """Process one tick through the state machine.

        Returns:
            List of action strings to execute on MT5:
            ``"OPEN_LONG"``, ``"OPEN_SHORT"``, ``"CLOSE_LONG"``,
            ``"CLOSE_SHORT"``
        """
        actions = []

        if np.isnan(z_score) or price <= 0:
            return actions

        # ---- 1. FLAT — look for entry ----
        if self.state == self.FLAT:
            if z_score < -self.zscore_entry:
                self.long_active = True
                self.long_entry = price
                self.state = self.INITIAL
                self._snapshot_params()
                actions.append("OPEN_LONG")
            elif z_score > self.zscore_entry:
                self.short_active = True
                self.short_entry = price
                self.state = self.INITIAL
                self._snapshot_params()
                actions.append("OPEN_SHORT")

        # ---- 2. INITIAL_TRADE — take profit or trigger hedge ----
        elif self.state == self.INITIAL:
            if self.long_active:
                pnl = (price - self.long_entry) / self.long_entry
                if pnl >= self.take_profit_pct:
                    self.long_active = False
                    self.state = self.FLAT
                    self._active = None
                    actions.append("CLOSE_LONG")
                elif pnl <= -self.hedge_trigger_pct:
                    # Initial trade is losing → open counter-hedge leg
                    self.short_active = True
                    self.short_entry = price
                    self.state = self.HEDGED
                    actions.append("OPEN_SHORT")
            elif self.short_active:
                pnl = (self.short_entry - price) / self.short_entry
                if pnl >= self.take_profit_pct:
                    self.short_active = False
                    self.state = self.FLAT
                    self._active = None
                    actions.append("CLOSE_SHORT")
                elif pnl <= -self.hedge_trigger_pct:
                    # Initial trade is losing → open counter-hedge leg
                    self.long_active = True
                    self.long_entry = price
                    self.state = self.HEDGED
                    actions.append("OPEN_LONG")

        # ---- 3. HEDGED_DUAL — winner selection ----
        elif self.state == self.HEDGED:
            long_pnl = (price - self.long_entry) / self.long_entry
            short_pnl = (self.short_entry - price) / self.short_entry

            # Long winning, Short losing → CUT SHORT, keep LONG
            if long_pnl >= self.win_profit_pct and short_pnl <= -self.cut_losing_pct:
                self.short_active = False
                self.state = self.SOLO
                self.survivor_peak = price
                actions.append("CLOSE_SHORT")
            # Short winning, Long losing → CUT LONG, keep SHORT
            elif short_pnl >= self.win_profit_pct and long_pnl <= -self.cut_losing_pct:
                self.long_active = False
                self.state = self.SOLO
                self.survivor_peak = price
                actions.append("CLOSE_LONG")
            # Both losing badly → EXIT ALL
            elif (long_pnl + short_pnl) < -2 * self.cut_losing_pct:
                self.long_active = False
                self.short_active = False
                self.state = self.FLAT
                self._active = None
                actions.extend(["CLOSE_LONG", "CLOSE_SHORT"])

        # ---- 4. SOLO_SURVIVOR — trailing stop ----
        elif self.state == self.SOLO:
            if self.long_active:
                self.survivor_peak = max(self.survivor_peak, price)
                drawdown = (self.survivor_peak - price) / self.survivor_peak
                if drawdown >= self.trail_stop_pct or z_score >= 0.0:
                    self.long_active = False
                    self.state = self.FLAT
                    self._active = None
                    actions.append("CLOSE_LONG")
            elif self.short_active:
                self.survivor_peak = min(self.survivor_peak, price)
                if self.survivor_peak > 0:
                    drawup = (price - self.survivor_peak) / self.survivor_peak
                else:
                    drawup = 0.0
                if drawup >= self.trail_stop_pct or z_score <= 0.0:
                    self.short_active = False
                    self.state = self.FLAT
                    self._active = None
                    actions.append("CLOSE_SHORT")

        return actions


# ===========================================================================
# MAIN REALTIME LOOP
# ===========================================================================


def _sync_positions_with_mt5(hedge_mgr):
    """Detect actual MT5 positions and sync state machine if a position
    was closed externally (e.g. by the broker's emergency stop-loss)."""
    positions = get_current_position(SYMBOL, magic=MAGIC_NUMBER)
    mt5_has_long = any(p.type == mt5.ORDER_TYPE_BUY for p in positions)
    mt5_has_short = any(p.type == mt5.ORDER_TYPE_SELL for p in positions)

    # Detect desync: state machine thinks a leg is active but MT5 closed it
    resync_needed = False
    if hedge_mgr.long_active and not mt5_has_long:
        print("[SYNC] ⚠ Long position was closed externally (emergency SL?)")
        resync_needed = True
    if hedge_mgr.short_active and not mt5_has_short:
        print("[SYNC] ⚠ Short position was closed externally (emergency SL?)")
        resync_needed = True

    if resync_needed:
        long_entry = 0.0
        short_entry = 0.0
        for p in positions:
            if p.type == mt5.ORDER_TYPE_BUY:
                long_entry = p.price_open
            elif p.type == mt5.ORDER_TYPE_SELL:
                short_entry = p.price_open
        hedge_mgr.sync_from_mt5(mt5_has_long, long_entry, mt5_has_short, short_entry)


def run_realtime_trader():
    initialize_mt5()
    print("\n" + "=" * 60)
    print(" 🔄 LIVE DUAL-HEDGE STATE MACHINE + RL PARAMETER TUNING")
    print("=" * 60)
    print(" • Strategy manages ALL entries, hedges, and exits")
    print(" • MT5 SL is emergency-only (3%) — state machine handles exits")
    print(" • RL tunes parameters dynamically each tick")
    print("=" * 60)

    is_rl = STRATEGY_TYPE == "rl_adaptive"
    bars_since_retrain = 0
    last_h1_time = None

    # ── Persistent DualHedge state machine ──
    hedge_mgr = LiveDualHedgeManager()

    # If using fixed DualHedge (non-RL), apply its configured parameters
    if STRATEGY_TYPE == "dual_hedge":
        hedge_mgr.update_params(
            {
                "zscore_entry": STRATEGY.zscore_entry,
                "hedge_trigger_pct": STRATEGY.hedge_trigger_loss_pct,
                "cut_losing_pct": STRATEGY.cut_losing_leg_pct,
                "win_profit_pct": STRATEGY.winning_leg_profit_pct,
                "trail_stop_pct": STRATEGY.trailing_stop_pct,
                "take_profit_pct": STRATEGY.take_profit_pct,
            }
        )

    # ── Sync from any existing MT5 positions (bot restart recovery) ──
    existing = get_current_position(SYMBOL, magic=MAGIC_NUMBER)
    if existing:
        ex_longs = [p for p in existing if p.type == mt5.ORDER_TYPE_BUY]
        ex_shorts = [p for p in existing if p.type == mt5.ORDER_TYPE_SELL]
        hedge_mgr.sync_from_mt5(
            has_long=bool(ex_longs),
            long_entry=ex_longs[0].price_open if ex_longs else 0.0,
            has_short=bool(ex_shorts),
            short_entry=ex_shorts[0].price_open if ex_shorts else 0.0,
        )
    else:
        print("[SYNC] No existing positions — starting FLAT.")

    # ── Pre-train RL agent on historical data ──
    if is_rl:
        _rl_train_on_history(STRATEGY)

    try:
        while True:
            # 1. Fetch historical bars from MT5
            rates = mt5.copy_rates_from_pos(SYMBOL, CALC_TIMEFRAME, 1, HISTORY_BARS)
            if rates is None or len(rates) < MIN_FEATURE_BARS:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Waiting for bar data..."
                )
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            df_hour = pd.DataFrame(rates)
            df_hour["time"] = pd.to_datetime(df_hour["time"], unit="s")
            df_hour["symbol"] = SYMBOL
            df_hour.rename(columns={"tick_volume": "volume"}, inplace=True)
            df_hour.dropna(inplace=True)

            # 2. Fetch live tick for instant intra-bar execution
            tick = mt5.symbol_info_tick(SYMBOL)
            if tick is None:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            current_price = (tick.ask + tick.bid) / 2.0
            live_time = datetime.now()

            # Append live tick as synthetic bar for real-time feature calculation
            live_bar = pd.DataFrame(
                [
                    {
                        "time": live_time,
                        "open": current_price,
                        "high": current_price,
                        "low": current_price,
                        "close": current_price,
                        "volume": 1.0,
                        "symbol": SYMBOL,
                    }
                ]
            )
            df_eval = pd.concat([df_hour, live_bar], ignore_index=True)

            # 3. Compute features (1H indicators evaluated at live price)
            df_featured = _prepare_feature_data(df_eval)
            if len(df_featured) < MIN_FEATURE_BARS:
                time.sleep(CHECK_INTERVAL_SECONDS)
                continue

            latest_z = (
                df_featured["ou_zscore"].iloc[-1]
                if "ou_zscore" in df_featured.columns
                else 0.0
            )

            # 4. Sync with MT5 — detect if broker closed a position externally
            _sync_positions_with_mt5(hedge_mgr)

            # 5. RL parameter tuning (update hedge_mgr params each tick)
            if is_rl:
                # Re-train only when a NEW H1 bar has closed
                current_h1_time = df_hour["time"].iloc[-1]
                if last_h1_time is not None and current_h1_time != last_h1_time:
                    bars_since_retrain += 1
                    if bars_since_retrain >= RETRAIN_EVERY_BARS:
                        print(
                            f"\n[RL RETRAIN] Re-training PPO on "
                            f"{len(df_featured)} bars..."
                        )
                        STRATEGY.generate_signals(df_featured)
                        bars_since_retrain = 0
                last_h1_time = current_h1_time

                params = _rl_get_params(STRATEGY, df_featured)
                if params:
                    hedge_mgr.update_params(params)

            # 6. Run DualHedge state machine tick
            actions = hedge_mgr.tick(current_price, latest_z)

            # 7. Execute MT5 orders based on state machine transitions
            time_str = live_time.strftime("%H:%M:%S")

            for act in actions:
                if act == "OPEN_LONG":
                    print(
                        f"[{time_str}] 🚀 OPEN LONG @ {current_price:,.2f} "
                        f"(Z={latest_z:+.2f})"
                    )
                    send_order(
                        SYMBOL,
                        mt5.ORDER_TYPE_BUY,
                        VOLUME,
                        sl_dist=current_price * EMERGENCY_SL_PCT,
                        tp_dist=0.0,  # NO TP — state machine manages exit
                    )

                elif act == "OPEN_SHORT":
                    print(
                        f"[{time_str}] 🔻 OPEN SHORT @ {current_price:,.2f} "
                        f"(Z={latest_z:+.2f})"
                    )
                    send_order(
                        SYMBOL,
                        mt5.ORDER_TYPE_SELL,
                        VOLUME,
                        sl_dist=current_price * EMERGENCY_SL_PCT,
                        tp_dist=0.0,  # NO TP — state machine manages exit
                    )

                elif act == "CLOSE_LONG":
                    positions = get_current_position(SYMBOL, magic=MAGIC_NUMBER)
                    for p in positions:
                        if p.type == mt5.ORDER_TYPE_BUY:
                            pnl_pct = (current_price - p.price_open) / p.price_open
                            tag = "✅ WIN" if pnl_pct > 0 else "❌ LOSS"
                            print(
                                f"[{time_str}] {tag} CLOSE LONG "
                                f"(PnL: {pnl_pct:+.2%})"
                            )
                            close_position(p)

                elif act == "CLOSE_SHORT":
                    positions = get_current_position(SYMBOL, magic=MAGIC_NUMBER)
                    for p in positions:
                        if p.type == mt5.ORDER_TYPE_SELL:
                            pnl_pct = (p.price_open - current_price) / p.price_open
                            tag = "✅ WIN" if pnl_pct > 0 else "❌ LOSS"
                            print(
                                f"[{time_str}] {tag} CLOSE SHORT "
                                f"(PnL: {pnl_pct:+.2%})"
                            )
                            close_position(p)

            # 8. Status display
            z_str = f"{latest_z:+.2f}" if not np.isnan(latest_z) else "N/A"
            status = hedge_mgr.status_line(current_price)
            print(
                f"[{time_str}] Price: {current_price:,.2f} | "
                f"Z: {z_str:>6} | {status}"
            )

            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n[STOPPED] Realtime Paper Trader shut down by user.")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    run_realtime_trader()
