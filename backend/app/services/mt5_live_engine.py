# backend/app/services/mt5_live_engine.py
"""
MIMIR MT5 Live Execution Engine Service.
Runs autonomous real-time trading loops in background threads, polling MT5 market
data, evaluating strategy signals (such as gold_math_scalper or gold_news_sentiment),
and automatically executing market orders with asymmetric dynamic SL/TP.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

from backend.app.integration.mt5_executor import (
    initialize_mt5,
    get_mt5_account_info,
    get_mt5_positions,
    send_mt5_order,
    close_mt5_positions,
    resolve_broker_symbol,
    LIVE_TRADING_ENABLED,
    DEFAULT_LOT_SIZE,
    DEFAULT_MAGIC_NUMBER,
)
import backend.app.integration.mt5_executor as mt5_exec
from bong_strats.indicators import OUCalibrator
from bong_strats.strategies.gold.gold_math_scalper import GoldMathScalperStrategy
from bong_strats.strategies.gold.gold_news_sentiment import GoldNewsSentimentStrategy

logger = logging.getLogger("mimir.mt5_live_engine")

# Global engine state
_engine_lock = threading.Lock()
_engine_running = False
_engine_thread: Optional[threading.Thread] = None

# Active live configuration
_live_config = {
    "strategy_id": "gold_math_scalper",
    "symbol": "XAUUSD",
    "timeframe": "M1",
    "lot_size": 0.01,
    "poll_interval": 3.0,
    "is_active": False,
    "live_trading_enabled": False,
    "max_open_positions": 1,
    "sl_atr_mult": 1.5,
    "tp_atr_mult": 3.0,
    "enable_mean_exit": True,
    "zscore_exit": 0.20,
    "enable_time_exit": True,
    "max_holding_minutes": 60.0,
}

# Live execution log buffer (recent 100 log lines)
_execution_logs = collections.deque(maxlen=100)

# Local price bar cache (e.g. 150 bars for indicator computation)
_bar_cache = collections.deque(maxlen=150)
_last_signal_time: Optional[str] = None
_last_signal_info: Dict[str, Any] = {}


def log_live_event(message: str, level: str = "INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "level": level, "message": message}
    _execution_logs.append(entry)
    if level == "ERROR":
        logger.error(f"[MT5_LIVE] {message}")
    elif level == "WARNING":
        logger.warning(f"[MT5_LIVE] {message}")
    else:
        logger.info(f"[MT5_LIVE] {message}")


def get_engine_status() -> Dict[str, Any]:
    """Returns the full real-time operational state for the UI."""
    with _engine_lock:
        is_running = _engine_running
        cfg = dict(_live_config)
    cfg["live_trading_enabled"] = mt5_exec.LIVE_TRADING_ENABLED

    account_info = get_mt5_account_info()
    positions = get_mt5_positions(symbol=cfg.get("symbol"))

    return {
        "engine_running": is_running,
        "live_trading_enabled": mt5_exec.LIVE_TRADING_ENABLED,
        "config": cfg,
        "account": account_info,
        "positions": positions,
        "logs": list(_execution_logs),
        "last_signal": _last_signal_info,
        "bar_cache_count": len(_bar_cache),
    }


def update_live_config(new_config: Dict[str, Any]) -> Dict[str, Any]:
    with _engine_lock:
        for k, v in new_config.items():
            if k in _live_config:
                _live_config[k] = v
        # Synchronize live trading authorization
        if "live_trading_enabled" in new_config:
            mt5_exec.LIVE_TRADING_ENABLED = bool(new_config["live_trading_enabled"])
            _live_config["live_trading_enabled"] = mt5_exec.LIVE_TRADING_ENABLED
        # Synchronize global lot size
        if "lot_size" in new_config:
            mt5_exec.DEFAULT_LOT_SIZE = float(new_config["lot_size"])
        log_live_event(f"Configuration updated: {new_config}")
        return dict(_live_config)


def _get_mt5_timeframe_constant(tf_str: str):
    if not mt5:
        return 1
    tf_map = {
        "M1": mt5.TIMEFRAME_M1,
        "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }
    return tf_map.get(tf_str.upper(), mt5.TIMEFRAME_M1)


def _live_trading_worker_loop():
    """Main background loop executing trading logic tick-by-tick."""
    global _engine_running, _last_signal_time, _last_signal_info

    log_live_event("MT5 Real-Time Engine starting worker loop...")
    if not initialize_mt5():
        log_live_event("Failed to connect to MT5 terminal during engine startup", "ERROR")
        _engine_running = False
        return

    # Instantiate strategies
    scalper = GoldMathScalperStrategy(mode="static")
    sentiment = GoldNewsSentimentStrategy(mode="static")

    while _engine_running:
        try:
            with _engine_lock:
                cfg = dict(_live_config)

            symbol_raw = cfg.get("symbol", "XAUUSD")
            resolved_symbol = resolve_broker_symbol(symbol_raw)
            tf_const = _get_mt5_timeframe_constant(cfg.get("timeframe", "M1"))
            lot_size = float(cfg.get("lot_size", 0.01))
            strategy_id = cfg.get("strategy_id", "gold_math_scalper")
            poll_interval = float(cfg.get("poll_interval", 3.0))
            max_positions = int(cfg.get("max_open_positions", 1))

            # Fetch recent candles from MT5
            rates = mt5.copy_rates_from_pos(resolved_symbol, tf_const, 0, 100)
            if rates is None or len(rates) < 30:
                log_live_event(f"Insufficient rates returned for {resolved_symbol}", "WARNING")
                time.sleep(poll_interval)
                continue

            df = pd.DataFrame(rates)
            df['datetime'] = pd.to_datetime(df['time'], unit='s', utc=True)
            df.set_index('datetime', inplace=True)

            # Check open positions on this symbol
            open_positions = get_mt5_positions(symbol=resolved_symbol)
            current_longs = [p for p in open_positions if p.get("type") in ("BUY", 0)]
            current_shorts = [p for p in open_positions if p.get("type") in ("SELL", 1)]

            # Compute strategy signals
            current_price = float(df['close'].iloc[-1])
            atr_series = df['high'].iloc[-14:] - df['low'].iloc[-14:]
            atr_val = float(atr_series.mean()) if len(df) >= 14 else 5.0
            if atr_val <= 0 or np.isnan(atr_val):
                atr_val = 5.0

            # -------------------------------------------------------------
            # Active Position Surveillance: Mean-Reversion Target & Time Exits
            # -------------------------------------------------------------
            if open_positions and strategy_id == "gold_math_scalper":
                enable_mean_exit = cfg.get("enable_mean_exit", True)
                zscore_exit_thresh = float(cfg.get("zscore_exit", 0.20))
                enable_time_exit = cfg.get("enable_time_exit", True)
                max_holding_mins = float(cfg.get("max_holding_minutes", 60.0))

                log_close = np.log(df['close'].where(df['close'] > 0, np.nan)).values
                calibrator = OUCalibrator()
                lookback = min(40, len(log_close) - 1)
                win = log_close[-lookback:]
                th, mu, sg = calibrator.calibrate(win)
                cur_zscore = calibrator.zscore(log_close[-1], mu, sg, th, sample_std=np.std(win))

                for pos in list(open_positions):
                    is_buy = pos.get("type") in ("BUY", 0)
                    entry_p = float(pos.get("price_open", current_price))
                    ticket = pos.get("ticket")
                    profit = float(pos.get("profit", 0.0))

                    open_epoch = pos.get("open_time_epoch")
                    elapsed_mins = (time.time() - open_epoch) / 60.0 if (open_epoch and open_epoch > 0) else 0.0

                    # Condition 1: Mean Reversion Equilibrium Target Reached (Banks early profit)
                    hit_mean_reversion = False
                    if enable_mean_exit:
                        if is_buy and cur_zscore >= -zscore_exit_thresh and current_price > entry_p:
                            hit_mean_reversion = True
                        elif not is_buy and cur_zscore <= zscore_exit_thresh and current_price < entry_p:
                            hit_mean_reversion = True

                    # Condition 2: Max Holding Time Reached without hitting full TP
                    hit_time_limit = False
                    if enable_time_exit and elapsed_mins >= max_holding_mins:
                        hit_time_limit = True

                    if hit_mean_reversion or hit_time_limit:
                        exit_type = "CLOSE_LONG" if is_buy else "CLOSE_SHORT"
                        if hit_mean_reversion:
                            exit_msg = f"Mean-Reversion Fair Equilibrium reached (Z={cur_zscore:+.2f}, Profit=+${profit:.2f})"
                        else:
                            exit_msg = f"Max Holding Time expired ({elapsed_mins:.1f}m >= {max_holding_mins:.0f}m limit, Profit=${profit:+.2f})"

                        log_live_event(f"[EXIT TRIGGERED] {exit_type} #{ticket} - {exit_msg}")
                        if mt5_exec.LIVE_TRADING_ENABLED:
                            c_res = close_mt5_positions(ticker=resolved_symbol, position_type="BUY" if is_buy else "SELL")
                            if c_res.get("success"):
                                log_live_event(f"SUCCESS: Closed position #{ticket} - {exit_msg}")
                                open_positions = [p for p in open_positions if p.get("ticket") != ticket]
                                current_longs = [p for p in open_positions if p.get("type") in ("BUY", 0)]
                                current_shorts = [p for p in open_positions if p.get("type") in ("SELL", 1)]
                            else:
                                log_live_event(f"FAILED to close position #{ticket}: {c_res.get('message')}", "ERROR")

            action = None
            reason = ""

            if strategy_id == "gold_math_scalper":
                result = scalper.generate_signals(df)
                if isinstance(result, pd.Series):
                    sig = int(result.iloc[-1]) if not result.empty else 0
                elif isinstance(result, pd.DataFrame):
                    sig = int(result['signal'].iloc[-1]) if 'signal' in result.columns and not result.empty else 0
                elif isinstance(result, dict):
                    raw_sig = result.get("signal", pd.Series(0, index=df.index))
                    sig = int(raw_sig.iloc[-1]) if not raw_sig.empty else 0
                else:
                    sig = 0
                
                _last_signal_info = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "strategy": "Gold Math Scalper",
                    "price": current_price,
                    "signal": sig,
                    "regime": "OU_ACTIVE",
                    "atr": round(atr_val, 2),
                }

                if sig == 1 and len(current_longs) < max_positions:
                    action = "OPEN_LONG"
                    reason = f"OU Mean-Reversion Oversold Dip (Price: {current_price})"
                elif sig == -1 and len(current_shorts) < max_positions:
                    action = "OPEN_SHORT"
                    reason = f"OU Mean-Reversion Overbought Peak (Price: {current_price})"

            elif strategy_id == "gold_news_sentiment":
                result = sentiment.generate_signals(df)
                if isinstance(result, pd.Series):
                    sig = int(result.iloc[-1]) if not result.empty else 0
                elif isinstance(result, pd.DataFrame):
                    sig = int(result['signal'].iloc[-1]) if 'signal' in result.columns and not result.empty else 0
                elif isinstance(result, dict):
                    raw_sig = result.get("signal", pd.Series(0, index=df.index))
                    sig = int(raw_sig.iloc[-1]) if not raw_sig.empty else 0
                else:
                    sig = 0
                
                _last_signal_info = {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "strategy": "Gold News Sentiment",
                    "price": current_price,
                    "signal": sig,
                    "atr": round(atr_val, 2),
                }

                if sig == 1 and len(current_longs) < max_positions:
                    action = "OPEN_LONG"
                    reason = f"Bullish Macro / Sentiment Surge (Price: {current_price})"
                elif sig == -1 and len(current_shorts) < max_positions:
                    action = "OPEN_SHORT"
                    reason = f"Bearish Macro / Sentiment Breakdown (Price: {current_price})"

            # Execution logic if action is triggered
            if action:
                # Calculate asymmetric stop and take profit
                sl_pct = (atr_val * 1.5 / current_price) * 100.0
                tp_pct = (atr_val * 3.5 / current_price) * 100.0

                log_live_event(f"[SIGNAL DETECTED] {action} on {resolved_symbol} - {reason}")
                
                # Check if live execution is authorized
                if mt5_exec.LIVE_TRADING_ENABLED:
                    order_res = send_mt5_order(
                        ticker=resolved_symbol,
                        action=action,
                        volume=lot_size,
                        sl_pct=sl_pct,
                        tp_pct=tp_pct,
                        comment=f"MIMIR {strategy_id[:12]}"
                    )
                    if order_res.get("success"):
                        log_live_event(f"SUCCESS: Order filled! Ticket #{order_res.get('ticket')} {action} {lot_size} lots")
                    else:
                        log_live_event(f"ORDER FAILED: {order_res.get('message')}", "ERROR")
                else:
                    log_live_event(f"[SIMULATION MODE] Signal received but LIVE_TRADING_ENABLED is False. No order placed.")

        except Exception as e:
            log_live_event(f"Exception in live loop: {e}", "ERROR")

        time.sleep(poll_interval)

    log_live_event("MT5 Real-Time Engine worker loop exited cleanly.")


def start_live_engine() -> Dict[str, Any]:
    global _engine_running, _engine_thread
    with _engine_lock:
        if _engine_running:
            return {"success": False, "message": "Engine is already running."}
        _engine_running = True
        _live_config["is_active"] = True
        _engine_thread = threading.Thread(target=_live_trading_worker_loop, daemon=True)
        _engine_thread.start()
        log_live_event("MT5 Live Engine started by user.")
        return {"success": True, "message": "MT5 Real-Time Trading Engine started."}


def stop_live_engine() -> Dict[str, Any]:
    global _engine_running
    with _engine_lock:
        if not _engine_running:
            return {"success": False, "message": "Engine is not running."}
        _engine_running = False
        _live_config["is_active"] = False
        log_live_event("MT5 Live Engine stop requested by user.")
        return {"success": True, "message": "MT5 Real-Time Trading Engine stopping..."}
