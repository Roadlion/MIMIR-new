import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.services.sector_rotation_service import SECTOR_ETFS, SUB_CATEGORY_TO_ETF

settings = get_settings()
schema = settings.mimir_schema

def run_granular_test():
    conn = get_db_connection()
    
    # Load daily price bars
    sql = f"""
        SELECT ticker, date, open, high, low, close, volume
        FROM {schema}.v_mimir_daily_ohlcv
        WHERE date >= '2025-01-01'
        ORDER BY ticker, date ASC
    """
    df = pd.read_sql(sql, conn)
    df['date'] = pd.to_datetime(df['date']).dt.date
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col], errors='coerce')
    bars = {t: grp.set_index('date').sort_index() for t, grp in df.groupby('ticker')}

    # Pre-compute ETF 5-day RS and 5-day Nominal Return
    spy_df = bars['SPY']
    etf_stats = {}  # (etf, date) -> (rs_5d, ret_5d)
    for etf in SECTOR_ETFS.keys():
        if etf not in bars:
            continue
        closes = bars[etf]['close']
        spy_closes = spy_df['close']
        common = closes.index.intersection(spy_closes.index).sort_values()
        ratios = closes.loc[common] / spy_closes.loc[common]
        rs_5d = (ratios / ratios.shift(5) - 1.0) * 100.0
        ret_5d = (closes.loc[common] / closes.loc[common].shift(5) - 1.0) * 100.0
        for dt in common[5:]:
            etf_stats[(etf, dt)] = (float(rs_5d.loc[dt]), float(ret_5d.loc[dt]))

    # Load alerts
    sql_alerts = f"""
        SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.target_price, s.stop_loss,
               s.catalyst_type, s.sentiment_score, s.conviction_score, s.created_at,
               COALESCE(si.asset_sub_category, 'UNKNOWN') as sector
        FROM {schema}.mimir_trade_signals s
        LEFT JOIN LATERAL (
            SELECT asset_sub_category
            FROM {schema}.mimir_sentiment_impacts
            WHERE ticker = s.ticker AND asset_sub_category IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 1
        ) si ON true
        WHERE s.signal_type = 'BUY'
          AND s.trigger_price IS NOT NULL AND s.trigger_price > 0
          AND s.created_at >= '2025-01-01'
        ORDER BY s.created_at ASC
    """
    df_alerts = pd.read_sql(sql_alerts, conn)
    conn.close()

    from scripts.backtest_sector_rotation_and_pre_earnings import simulate_trade, compute_metrics

    cohort_stealth = []   # RS >= 1.0% AND Ret_5d <= 3.0% (Quiet stealth base)
    cohort_extended = [] # RS >= 1.0% AND Ret_5d > 3.0% (Chasing momentum)
    cohort_pre_earnings = [] # PRE_EARNINGS_BEAT
    cohort_high_conviction_stealth = []

    for _, row in df_alerts.iterrows():
        ticker = row['ticker'].strip().upper()
        if ticker not in bars:
            continue
        dt = pd.to_datetime(row['created_at']).date()
        entry_p = float(row['trigger_price'])
        cat_type = str(row['catalyst_type'] or '')
        conv = float(row['conviction_score'] or 0.0)
        sent = float(row['sentiment_score'] or 0.0)

        sector = str(row['sector']).upper()
        etf = SUB_CATEGORY_TO_ETF.get(sector)
        stats = etf_stats.get((etf, dt))

        df_hist = bars[ticker].loc[:dt]
        if len(df_hist) < 14:
            continue

        tr = pd.concat([
            df_hist['high'] - df_hist['low'],
            (df_hist['high'] - df_hist['close'].shift(1)).abs(),
            (df_hist['low'] - df_hist['close'].shift(1)).abs()
        ], axis=1).max(axis=1)
        atr = float(tr.tail(14).mean())
        if np.isnan(atr) or atr <= 0:
            atr = entry_p * 0.03

        # Home Run execution
        sl_hr = entry_p - max(0.040 * entry_p, min(0.075 * entry_p, 1.5 * atr))
        tp_hr = entry_p + max(0.120 * entry_p, min(0.250 * entry_p, 3.0 * atr))
        tp_hr = max(tp_hr, entry_p + (entry_p - sl_hr) * 2.5)

        res = simulate_trade(ticker, dt, entry_p, tp_hr, sl_hr, bars, max_holding_days=15)
        if not res:
            continue

        if cat_type == "PRE_EARNINGS_BEAT":
            cohort_pre_earnings.append(res)

        if stats:
            rs_5d, ret_5d = stats
            if rs_5d >= 1.0 and ret_5d <= 3.0:
                cohort_stealth.append(res)
                if conv >= 0.65 or abs(sent) >= 0.35:
                    cohort_high_conviction_stealth.append(res)
            elif rs_5d >= 1.0 and ret_5d > 3.0:
                cohort_extended.append(res)

    print("\n" + "=" * 105)
    print("GRANULAR COHORT ANALYSIS: STEALTH BASE VS. EXTENDED MOMENTUM VS. PRE-EARNINGS")
    print("=" * 105)
    print(f"{'Cohort':<35} {'Trades':<8} {'Wins':<6} {'Losses':<8} {'Win Rate %':<12} {'Avg PnL %':<12} {'Profit Factor':<15} {'Payoff R/R':<12} {'Max DD %'}")
    print("-" * 105)
    for name, c in [
        ("1. Chasing Extended Sector (>+3% 5D)", cohort_extended),
        ("2. Stealth Base (RS>=1%, Ret<=3%)", cohort_stealth),
        ("3. High Conviction Stealth Base", cohort_high_conviction_stealth),
        ("4. Pre-Earnings Beat Engine", cohort_pre_earnings)
    ]:
        m = compute_metrics(c)
        print(f"{name:<35} {m['trades']:<8} {m['wins']:<6} {m['losses']:<8} {m['win_rate']:<12} {m['avg_pnl']:+6.2f}%      {m['profit_factor']:<15} {m['payoff_ratio']:<12} {m['max_dd']:.2f}%")
    print("=" * 105)

if __name__ == "__main__":
    run_granular_test()
