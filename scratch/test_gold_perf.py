# scratch/test_gold_perf.py
import sys
import os
sys.path.insert(0, os.path.abspath("."))
import pandas as pd
import numpy as np
from backend.app.analytics.strategy_backtester import run_strategy_simulation, load_candles

print("=" * 70)
print("MIMIR GOLD QUANT PERFORMANCE LAB")
print("=" * 70)

timeframes = ["1h", "15m", "1d"]
for tf in timeframes:
    df = load_candles("XAUUSD", timeframe=tf, limit=1000)
    if df is not None and len(df) > 0:
        p_first = df["close"].iloc[0]
        p_last = df["close"].iloc[-1]
        t_first = str(df["timestamp"].iloc[0])[:16]
        t_last = str(df["timestamp"].iloc[-1])[:16]
        print(f"Data Feed [{tf}]: {len(df)} bars | {t_first} -> {t_last} | ${p_first:.2f} -> ${p_last:.2f} ({(p_last/p_first - 1)*100:+.2f}%)")
    else:
        print(f"Data Feed [{tf}]: Unable to fetch")

print("\n" + "=" * 70)
print("STRATEGY 1: Gold Math Scalper Performance Matrix")
print("=" * 70)

param_sets_math = [
    {"label": "Baseline Defaults", "params": {}},
    {"label": "Tight Asymmetric (1:2.5 RR)", "params": {"zscore_entry": 1.6, "zscore_exit": 0.15, "stop_atr_mult": 1.0, "tp_atr_mult": 2.5, "max_half_life_mult": 1.8}},
    {"label": "High-Conviction Extremes (|Z|>=2.2)", "params": {"zscore_entry": 2.2, "zscore_exit": 0.3, "stop_atr_mult": 1.5, "tp_atr_mult": 3.0, "max_half_life_mult": 2.5}},
    {"label": "Fast Scalper (Aggressive Time Decay)", "params": {"zscore_entry": 1.5, "zscore_exit": 0.1, "stop_atr_mult": 0.8, "tp_atr_mult": 2.0, "max_half_life_mult": 1.2}},
]

for pset in param_sets_math:
    try:
        res = run_strategy_simulation(
            strategy_id="gold_math_scalper",
            tickers_str="XAUUSD",
            timeframe="1h",
            initial_capital=10000.0,
            custom_params=pset["params"]
        )
        m = res["metrics"]
        trades = res["trades"]
        win_count = sum(1 for t in trades if (t.get("pnl_usd") or 0) > 0)
        loss_count = sum(1 for t in trades if (t.get("pnl_usd") or 0) < 0)
        win_rate = (win_count / max(1, len(trades))) * 100
        gross_win = sum(t.get("pnl_usd", 0) for t in trades if (t.get("pnl_usd") or 0) > 0)
        gross_loss = abs(sum(t.get("pnl_usd", 0) for t in trades if (t.get("pnl_usd") or 0) < 0))
        profit_factor = (gross_win / max(1e-4, gross_loss)) if gross_loss > 0 else 99.9

        print(f"[{pset['label']}]")
        print(f"  • Return: {m['annualized_return']:+.2f}% | Sharpe: {m['sharpe']:.2f} | MaxDD: {m['max_drawdown']:.2f}%")
        print(f"  • Trades: {len(trades)} | WinRate: {win_rate:.1f}% ({win_count}W / {loss_count}L) | ProfitFactor: {profit_factor:.2f}")
    except Exception as e:
        print(f"[{pset['label']}] Simulation error: {e}")

print("\n" + "=" * 70)
print("STRATEGY 2: Gold Macro & News Sentiment Performance Matrix")
print("=" * 70)

param_sets_news = [
    {"label": "Baseline Defaults", "params": {}},
    {"label": "High-Significance Shock Filter", "params": {"sentiment_threshold": 0.35, "sentiment_decay_half_life": 20, "stop_loss_atr_mult": 1.8, "take_profit_atr_mult": 4.5}},
    {"label": "Trend Riding Macro Bias", "params": {"sentiment_threshold": 0.18, "sentiment_decay_half_life": 35, "stop_loss_atr_mult": 2.5, "take_profit_atr_mult": 6.0, "exit_band": 0.03}},
    {"label": "Rapid News Scalp", "params": {"sentiment_threshold": 0.22, "sentiment_decay_half_life": 8, "stop_loss_atr_mult": 1.2, "take_profit_atr_mult": 2.5, "exit_band": 0.08}},
]

for pset in param_sets_news:
    try:
        res = run_strategy_simulation(
            strategy_id="gold_news_sentiment",
            tickers_str="XAUUSD",
            timeframe="1h",
            initial_capital=10000.0,
            custom_params=pset["params"]
        )
        m = res["metrics"]
        trades = res["trades"]
        win_count = sum(1 for t in trades if (t.get("pnl_usd") or 0) > 0)
        loss_count = sum(1 for t in trades if (t.get("pnl_usd") or 0) < 0)
        win_rate = (win_count / max(1, len(trades))) * 100
        gross_win = sum(t.get("pnl_usd", 0) for t in trades if (t.get("pnl_usd") or 0) > 0)
        gross_loss = abs(sum(t.get("pnl_usd", 0) for t in trades if (t.get("pnl_usd") or 0) < 0))
        profit_factor = (gross_win / max(1e-4, gross_loss)) if gross_loss > 0 else 99.9

        print(f"[{pset['label']}]")
        print(f"  • Return: {m['annualized_return']:+.2f}% | Sharpe: {m['sharpe']:.2f} | MaxDD: {m['max_drawdown']:.2f}%")
        print(f"  • Trades: {len(trades)} | WinRate: {win_rate:.1f}% ({win_count}W / {loss_count}L) | ProfitFactor: {profit_factor:.2f}")
    except Exception as e:
        print(f"[{pset['label']}] Simulation error: {e}")

print("\n" + "=" * 70)
print("MULTI-TIMEFRAME STRESS TEST: 15m (Scalp) vs 1d (Macro Swing)")
print("=" * 70)

for tf in ["15m", "1d"]:
    print(f"\n--- TIMEFRAME: {tf} ---")
    for strat in ["gold_math_scalper", "gold_news_sentiment"]:
        try:
            r = run_strategy_simulation(strategy_id=strat, tickers_str="XAUUSD", timeframe=tf)
            m = r["metrics"]
            trades = r["trades"]
            win_count = sum(1 for t in trades if (t.get("pnl_usd") or 0) > 0)
            wr = (win_count / max(1, len(trades))) * 100
            print(f"[{strat} @ {tf}] Return: {m['annualized_return']:+.2f}% | Sharpe: {m['sharpe']:.2f} | MaxDD: {m['max_drawdown']:.2f}% | Trades: {len(trades)} | WinRate: {wr:.1f}%")
        except Exception as e:
            print(f"[{strat} @ {tf}] Error: {e}")
