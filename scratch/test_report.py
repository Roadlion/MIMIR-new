# scratch/test_report.py
import sys, os
sys.path.insert(0, os.path.abspath("."))
from backend.app.analytics.strategy_backtester import run_strategy_simulation, load_candles

timeframes = [
    ("1d", "Daily Macro Swing"),
    ("1h", "Hourly Intraday"),
    ("15m", "15-Minute Micro Scalp"),
]

for tf, label in timeframes:
    df = load_candles("XAUUSD", timeframe=tf, limit=1000)
    p_start = df["close"].iloc[0]
    p_end = df["close"].iloc[-1]
    b_ret = (p_end / p_start - 1.0) * 100
    d_start = str(df["timestamp"].iloc[0])[:16]
    d_end = str(df["timestamp"].iloc[-1])[:16]
    print(f"=== {label} ({tf}) ===")
    print(f"  Exact Window : {d_start} to {d_end} ({len(df)} bars)")
    print(f"  Gold Price   : ${p_start:.2f} -> ${p_end:.2f} (Buy & Hold: {b_ret:+.2f}%)")

    for s in ["gold_news_sentiment", "gold_math_scalper"]:
        r = run_strategy_simulation(s, "XAUUSD", timeframe=tf)
        m = r["metrics"]
        trades = r["trades"]
        wins = sum(1 for t in trades if (t.get("pnl_usd") or 0) > 0)
        losses = sum(1 for t in trades if (t.get("pnl_usd") or 0) < 0)
        wr = (wins / max(1, len(trades))) * 100
        gross_win = sum(t.get("pnl_usd", 0) for t in trades if (t.get("pnl_usd") or 0) > 0)
        gross_loss = abs(sum(t.get("pnl_usd", 0) for t in trades if (t.get("pnl_usd") or 0) < 0))
        pf = (gross_win / max(1e-4, gross_loss)) if gross_loss > 0 else 99.9

        long_trades = [t for t in trades if "Long" in t.get("action", "") or "Exit Long" in t.get("action", "")]
        short_trades = [t for t in trades if "Short" in t.get("action", "") or "Cover Short" in t.get("action", "")]

        print(f"  [{s}]:")
        print(f"    • Annualized Return: {m['annualized_return']:+.2f}%")
        print(f"    • Sharpe Ratio     : {m['sharpe']:.2f}")
        print(f"    • Max Drawdown     : {m['max_drawdown']:.2f}%")
        print(f"    • Win Rate         : {wr:.1f}% ({wins} Wins / {losses} Losses)")
        print(f"    • Profit Factor    : {pf:.2f}")
        print(f"    • Total Trades     : {len(trades)} (Long trades: {len(long_trades)}, Short trades: {len(short_trades)})")
    print()
