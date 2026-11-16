# scratch/test_asym.py
import sys, os
sys.path.insert(0, os.path.abspath("."))
from backend.app.analytics.strategy_backtester import run_strategy_simulation

print("--- SYMMETRIC DEFAULTS ---")
r_sym = run_strategy_simulation("gold_news_sentiment", "XAUUSD", timeframe="1h")
m_sym = r_sym["metrics"]
win_sym = sum(1 for t in r_sym["trades"] if (t.get("pnl_usd") or 0) > 0)
wr_sym = (win_sym / max(1, len(r_sym["trades"]))) * 100
print(f"Symmetric: Return={m_sym['annualized_return']:+.2f}% | Sharpe={m_sym['sharpe']:.2f} | WinRate={wr_sym:.1f}% ({win_sym}W/{len(r_sym['trades'])-win_sym}L) | Trades={len(r_sym['trades'])}")

print("\n--- ASYMMETRIC (Long TP 4.5/SL 2.0 vs Short TP 2.0/SL 1.2) ---")
r_asym = run_strategy_simulation(
    "gold_news_sentiment",
    "XAUUSD",
    timeframe="1h",
    custom_params={
        "long_sl_atr_mult": 2.0,
        "long_tp_atr_mult": 4.5,
        "short_sl_atr_mult": 1.2,
        "short_tp_atr_mult": 2.0,
        "long_sentiment_threshold": 0.20,
        "short_sentiment_threshold": 0.30,
    }
)
m_asym = r_asym["metrics"]
win_asym = sum(1 for t in r_asym["trades"] if (t.get("pnl_usd") or 0) > 0)
wr_asym = (win_asym / max(1, len(r_asym["trades"]))) * 100
print(f"Asymmetric: Return={m_asym['annualized_return']:+.2f}% | Sharpe={m_asym['sharpe']:.2f} | WinRate={wr_asym:.1f}% ({win_asym}W/{len(r_asym['trades'])-win_asym}L) | Trades={len(r_asym['trades'])}")

print("\n" + "=" * 50)
print("1D DAILY TIMEFRAME ASYMMETRY COMPARISON")
print("=" * 50)
r_sym1d = run_strategy_simulation("gold_news_sentiment", "XAUUSD", timeframe="1d")
m_sym1d = r_sym1d["metrics"]
win_sym1d = sum(1 for t in r_sym1d["trades"] if (t.get("pnl_usd") or 0) > 0)
wr_sym1d = (win_sym1d / max(1, len(r_sym1d["trades"]))) * 100

r_asym1d = run_strategy_simulation(
    "gold_news_sentiment",
    "XAUUSD",
    timeframe="1d",
    custom_params={
        "long_sl_atr_mult": 2.0,
        "long_tp_atr_mult": 5.0,
        "short_sl_atr_mult": 1.2,
        "short_tp_atr_mult": 2.0,
        "long_sentiment_threshold": 0.18,
        "short_sentiment_threshold": 0.30,
    }
)
m_asym1d = r_asym1d["metrics"]
win_asym1d = sum(1 for t in r_asym1d["trades"] if (t.get("pnl_usd") or 0) > 0)
wr_asym1d = (win_asym1d / max(1, len(r_asym1d["trades"]))) * 100

print(f"1D Symmetric : Return={m_sym1d['annualized_return']:+.2f}% | Sharpe={m_sym1d['sharpe']:.2f} | WinRate={wr_sym1d:.1f}%")
print(f"1D Asymmetric: Return={m_asym1d['annualized_return']:+.2f}% | Sharpe={m_asym1d['sharpe']:.2f} | WinRate={wr_asym1d:.1f}%")
