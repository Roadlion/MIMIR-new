# scripts/standalone_mt5_live_trader.py
"""
Solely Dedicated Standalone Local MT5 Live Trading Script.
Runs directly on your local machine with ZERO database or web app requirements.

Features:
  - Connects directly to MT5 Terminal via MetaTrader5 API.
  - Interactive menu: pick exact asset(s) and exact strategy (or ALL).
  - Streams tick data into an in-memory sliding window cache.
  - Evaluates custom strategies tick-by-tick.
  - Sends direct real BUY/SELL market orders to MT5 terminal (mt5.order_send).
  - SUPPORTS PAIR TRADING (Statistical Arbitrage) via custom screener or manual pairs.
"""

import collections
import os
import sys
import time
from datetime import datetime
import numpy as np
import pandas as pd

# Add project root and backend to sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_ROOT = os.path.join(PROJECT_ROOT, "backend")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if BACKEND_ROOT not in sys.path:
    sys.path.insert(0, BACKEND_ROOT)

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[CRITICAL] MetaTrader5 package not installed. Run: pip install MetaTrader5")
    sys.exit(1)

from backend.app.analytics.user_strategies import USER_STRATEGY_REGISTRY
from backend.app.analytics.user_strategies.static_strategies import BACKTEST_STRATEGY_REGISTRY
from backend.app.analytics.user_strategies.indicators import (
    VolatilityModel,
    compute_bollinger_features,
    compute_ou_features,
    compute_rsi,
    compute_volatility_regime,
    KalmanPairEstimator,
    compute_adf_stat
)
from backend.app.analytics.user_strategies.pair_screener import screen_pairs, UNIVERSES
from backend.app.integration.mt5_executor import (
    close_mt5_positions,
    get_mt5_account_info,
    get_mt5_positions,
    initialize_mt5,
    resolve_broker_symbol,
    send_mt5_order,
)

# =====================================================================
# CONFIGURATION
# =====================================================================
DEFAULT_TICKERS = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "AMZN",
    "META", "TSLA", "AMD", "MU", "BTCUSD", "XAUUSD",
]

POLL_INTERVAL_SECONDS = 10   # 10-second tick polling loop
DEFAULT_LOT_SIZE = 0.01      # Volume per trade (0.01 micro-lots)
MAGIC_NUMBER = 202608        # Magic number for MIMIR orders
SL_PCT = 2.0                 # Stop Loss % (0.0 = strategy manages SL)
TP_PCT = 4.0                 # Take Profit % (0.0 = strategy manages TP)

# In-memory sliding price window cache (100 bars per ticker)
price_cache = collections.defaultdict(lambda: collections.deque(maxlen=100))
strategy_instances = {}

STRATEGY_DESCRIPTIONS = {
    "dual_hedge": "DualHedge — Dual-leg statistical hedging (FLAT → INITIAL → HEDGED → SOLO)",
    "mean_reversion": "MeanReversion — OU z-score mean-reversion (buy oversold, sell overbought)",
    "bollinger_bands": "BollingerBands — Bollinger Bands + RSI + Volatility Regime adaptation",
    "pair_stat_arbitrage": "PairStatArbitrage — Statistical Arbitrage (Pairs Trading)",
}

# Add backtest strategies (like pair_stat_arbitrage) to registry temporarily for the menu
FULL_REGISTRY = {**USER_STRATEGY_REGISTRY, **BACKTEST_STRATEGY_REGISTRY}

class LivePairTrader:
    def __init__(self, primary: str, hedge: str):
        self.primary = primary
        self.hedge = hedge
        self.b_primary = resolve_broker_symbol(primary)
        self.b_hedge = resolve_broker_symbol(hedge)
        self.cache_p1 = collections.deque(maxlen=100)
        self.cache_p2 = collections.deque(maxlen=100)
        
        self.kalman = KalmanPairEstimator(delta=1e-4, R=1e-3)
        self.position = 0.0
        self.active_beta = 0.0
        self.bars_held = 0

    def tick(self):
        rates1 = mt5.copy_rates_from_pos(self.b_primary, mt5.TIMEFRAME_M1, 0, 1)
        rates2 = mt5.copy_rates_from_pos(self.b_hedge, mt5.TIMEFRAME_M1, 0, 1)
        
        if rates1 is None or rates2 is None or len(rates1) == 0 or len(rates2) == 0: 
            return
            
        t1, t2 = rates1[0]['time'], rates2[0]['time']
        
        if len(self.cache_p1) == 0 or self.cache_p1[-1]['time'] < t1:
            self.cache_p1.append(rates1[0])
            self.cache_p2.append(rates2[0])
            if self.position != 0.0:
                self.bars_held += 1
        else:
            self.cache_p1[-1] = rates1[0]
            self.cache_p2[-1] = rates2[0]
            
        if len(self.cache_p1) < 20:
            return 
            
        df1 = pd.DataFrame(list(self.cache_p1))
        df2 = pd.DataFrame(list(self.cache_p2))
        
        y = np.log(df1['close'].values)
        x = np.log(df2['close'].values)
        
        alpha, beta, spread, spread_std, zscore = self.kalman.fit_series(y, x)
        
        curr_z = zscore[-1]
        curr_beta = beta[-1]
        adf = compute_adf_stat(spread)
        
        timestamp = datetime.now().strftime('%H:%M:%S')
        action = None
        
        ENTRY_Z = 2.0
        EXIT_Z = 0.0
        STOP_Z = 3.5
        MAX_BARS = 30
        
        if self.position != 0:
            if abs(curr_z) >= STOP_Z:
                action = "STOP_LOSS"
            elif self.bars_held >= MAX_BARS:
                action = "TIMEOUT_EXIT"
            elif self.position > 0 and curr_z >= EXIT_Z:
                action = "TARGET_REVERSION_EXIT"
            elif self.position < 0 and curr_z <= -EXIT_Z:
                action = "TARGET_REVERSION_EXIT"
                
            if action:
                self.execute_trade(action, 0.0)
                self.position = 0.0
                self.bars_held = 0
        else:
            if adf <= -2.0:
                if curr_z >= ENTRY_Z:
                    action = "ENTRY_SHORT_SPREAD"
                    self.execute_trade(action, -1.0, curr_beta)
                    self.position = -1.0
                    self.active_beta = curr_beta
                    self.bars_held = 0
                elif curr_z <= -ENTRY_Z:
                    action = "ENTRY_LONG_SPREAD"
                    self.execute_trade(action, 1.0, curr_beta)
                    self.position = 1.0
                    self.active_beta = curr_beta
                    self.bars_held = 0
                    
        if action:
            print(f"\n⚡ [{timestamp}] {action} | {self.primary}/{self.hedge} | Z: {curr_z:.2f} | Beta: {curr_beta:.3f}")

    def execute_trade(self, reason, new_pos, beta=0.0):
        if new_pos == 0.0:
            send_mt5_order(self.b_primary, "CLOSE_ALL", DEFAULT_LOT_SIZE, magic=MAGIC_NUMBER, comment=reason)
            send_mt5_order(self.b_hedge, "CLOSE_ALL", DEFAULT_LOT_SIZE * self.active_beta, magic=MAGIC_NUMBER, comment=reason)
        elif new_pos == 1.0:
            send_mt5_order(self.b_primary, "OPEN_LONG", DEFAULT_LOT_SIZE, magic=MAGIC_NUMBER, comment=reason)
            send_mt5_order(self.b_hedge, "OPEN_SHORT", DEFAULT_LOT_SIZE * beta, magic=MAGIC_NUMBER, comment=reason)
        elif new_pos == -1.0:
            send_mt5_order(self.b_primary, "OPEN_SHORT", DEFAULT_LOT_SIZE, magic=MAGIC_NUMBER, comment=reason)
            send_mt5_order(self.b_hedge, "OPEN_LONG", DEFAULT_LOT_SIZE * beta, magic=MAGIC_NUMBER, comment=reason)


def select_strategy() -> list:
    strat_names = list(STRATEGY_DESCRIPTIONS.keys())
    print("\n" + "=" * 65)
    print("🧠 SELECT STRATEGY")
    print("=" * 65)
    for i, name in enumerate(strat_names, 1):
        desc = STRATEGY_DESCRIPTIONS.get(name, name)
        print(f"  [{i}] {desc}")
    print("=" * 65)

    while True:
        raw = input("Select (e.g. 1 or 1,3): ").strip()
        try:
            indices = [int(x.strip()) for x in raw.split(",")]
            chosen = []
            for idx in indices:
                if 1 <= idx <= len(strat_names):
                    chosen.append(strat_names[idx - 1])
            if chosen:
                return chosen
        except ValueError:
            pass
        print("  ⚠ Invalid input. Try again.")


def select_tickers(available_tickers: list) -> list:
    print("\n" + "=" * 65)
    print("🎯 SELECT ASSET(S) TO TRADE")
    print("=" * 65)
    for i, ticker in enumerate(available_tickers, 1):
        print(f"  [{i}] {ticker}")
    print(f"  [A] ALL TICKERS ({len(available_tickers)} assets)")
    print("=" * 65)

    while True:
        raw = input("Select (e.g. 1 or 1,3,5 or A for all): ").strip().upper()
        if raw == "A":
            return available_tickers
        try:
            indices = [int(x.strip()) for x in raw.split(",")]
            chosen = []
            for idx in indices:
                if 1 <= idx <= len(available_tickers):
                    chosen.append(available_tickers[idx - 1])
            if chosen:
                return chosen
        except ValueError:
            pass
        print("  ⚠ Invalid input. Try again.")


def fetch_latest_tick(broker_symbol: str, display_symbol: str):
    rates = mt5.copy_rates_from_pos(broker_symbol, mt5.TIMEFRAME_M1, 0, 1)
    if rates is not None and len(rates) > 0:
        bar = rates[0]
        bar_time = datetime.fromtimestamp(int(bar['time'])).strftime('%Y-%m-%d %H:%M:%S')
        tick_data = {
            'timestamp': bar_time,
            'open': float(bar['open']),
            'high': float(bar['high']),
            'low': float(bar['low']),
            'close': float(bar['close']),
            'volume': float(bar['tick_volume']),
        }
        deque_cache = price_cache[display_symbol]
        if len(deque_cache) == 0 or deque_cache[-1]['timestamp'] < bar_time:
            deque_cache.append(tick_data)
            return True
        else:
            deque_cache[-1] = tick_data
            return True
    return False

def process_and_trade(ticker: str):
    deque_data = price_cache[ticker]
    if len(deque_data) < 10: return

    df = pd.DataFrame(list(deque_data))
    df = compute_ou_features(df, lookback=min(50, len(df) - 1))
    df = compute_bollinger_features(df)
    df["rsi_14"] = compute_rsi(df["close"])
    df["ewma_vol"] = VolatilityModel.ewma(df["close"].pct_change(), span=20)
    df["volatility_regime"] = compute_volatility_regime(df)

    latest = df.iloc[-1]
    price = float(latest["close"])

    features = {
        "ou_zscore": float(latest["ou_zscore"]) if not pd.isna(latest["ou_zscore"]) else 0.0,
        "ewma_vol": float(latest["ewma_vol"]) if not pd.isna(latest["ewma_vol"]) else 0.01,
        "bb_upper": float(latest.get("bb_upper", price)),
        "bb_lower": float(latest.get("bb_lower", price)),
        "bb_middle": float(latest.get("bb_middle", price)),
        "rsi_14": float(latest.get("rsi_14", 50.0)),
        "volatility_regime": str(latest.get("volatility_regime", "STABLE")),
    }

    strats = strategy_instances.get(ticker, {})
    for strat_name, strategy in strats.items():
        try:
            actions = strategy.tick(price, features)
            for action in actions:
                print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S')}] SIGNAL FIRED! {ticker} | {strat_name} | {action}")
                send_mt5_order(
                    ticker=ticker, action=action, volume=DEFAULT_LOT_SIZE,
                    sl_pct=SL_PCT if action.startswith("OPEN") else 0.0,
                    tp_pct=TP_PCT if action.startswith("OPEN") else 0.0,
                    magic=MAGIC_NUMBER, comment=f"MIMIR:{strat_name[:12]}"
                )
        except Exception as e:
            pass


def main():
    print("=" * 65)
    print("🚀 MIMIR STANDALONE LOCAL MT5 LIVE TRADER")
    print("=" * 65)

    if not initialize_mt5():
        print("[CRITICAL] Could not connect to MetaTrader 5 terminal. Make sure MT5 is open!")
        sys.exit(1)

    chosen_strategies = select_strategy()
    
    pair_traders = []
    single_tickers = []
    
    if "pair_stat_arbitrage" in chosen_strategies:
        print("\n" + "=" * 65)
        print("⚖️ PAIR TRADING SETUP")
        print("=" * 65)
        print("  [1] Screen market for best highly correlated pairs")
        print("  [2] Enter specific pairs manually (e.g. XAUUSD:XAGUSD)")
        choice = input("Select 1 or 2: ").strip()
        
        if choice == "1":
            print(f"Available universes: {', '.join(UNIVERSES.keys())}")
            uni = input("Enter universe (default 'nasdaq'): ").strip() or "nasdaq"
            print(f"\nScreening {uni}...")
            top_pairs = screen_pairs(uni, timeframe="M15", bars=500, top_n=3)
            if not top_pairs.empty:
                print("\nTop Pairs Found:")
                print(top_pairs[['primary', 'hedge', 'correlation', 'adf_t_stat', 'score']])
                for _, row in top_pairs.iterrows():
                    pair_traders.append(LivePairTrader(row['primary'], row['hedge']))
        else:
            pairs_raw = input("Enter comma-separated pairs (e.g. XAUUSD:XAGUSD, EURUSD:GBPUSD): ").strip()
            for p in pairs_raw.split(","):
                if ":" in p:
                    p1, p2 = p.split(":")
                    pair_traders.append(LivePairTrader(p1.strip().upper(), p2.strip().upper()))

        # Remove from single processing
        chosen_strategies.remove("pair_stat_arbitrage")
        
    if chosen_strategies:
        resolved_map = {}
        for ticker in DEFAULT_TICKERS:
            b_sym = resolve_broker_symbol(ticker)
            if b_sym: resolved_map[ticker] = b_sym
        
        available = list(resolved_map.keys())
        chosen_tickers = select_tickers(available)
        
        for sym in chosen_tickers:
            single_tickers.append(sym)
            strategy_instances[sym] = {}
            for name in chosen_strategies:
                cls = FULL_REGISTRY[name]
                strategy_instances[sym][name] = cls(mode="adaptive")
                
    print("\n[RUNNING] Entering live tick trading loop...")
    
    try:
        while True:
            time.sleep(POLL_INTERVAL_SECONDS)
            print(f"\n--- Tick [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---")
            
            # 1. Process Single Asset Strategies
            for sym in single_tickers:
                b_sym = resolve_broker_symbol(sym)
                if fetch_latest_tick(b_sym, sym):
                    process_and_trade(sym)
                    
            # 2. Process Pair Trading Strategies
            for p_trader in pair_traders:
                p_trader.tick()
                
            positions = get_mt5_positions()
            print(f"Active MT5 Open Positions: {len(positions)}")
            
    except KeyboardInterrupt:
        print("\n[STOPPING] Live trader terminated by user.")
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    main()
