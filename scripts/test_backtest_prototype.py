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

def run_test():
    conn = get_db_connection()
    cur = conn.cursor()
    
    print("1. Loading daily bars for Sector ETFs and SPY...")
    all_etfs = list(SECTOR_ETFS.keys()) + ["SPY"]
    sql_etf = f"""
        SELECT ticker, date, close
        FROM {schema}.v_mimir_daily_ohlcv
        WHERE ticker = ANY(%s)
        ORDER BY date ASC
    """
    df_etfs = pd.read_sql(sql_etf, conn, params=(all_etfs,))
    piv = df_etfs.pivot(index='date', columns='ticker', values='close').ffill()
    print(f"Loaded {len(piv)} daily dates for {len(piv.columns)} ETFs.")

    # Vectorized 5-day RS vs SPY for each sector ETF
    rs_matrix = pd.DataFrame(index=piv.index)
    spy_close = piv['SPY']
    for etf in SECTOR_ETFS.keys():
        if etf in piv.columns:
            ratio = piv[etf] / spy_close
            rs_matrix[etf] = (ratio / ratio.shift(5) - 1.0) * 100.0
    print("Calculated historical 5-day Relative Strength matrix.")

    print("\n2. Fetching historical trade alerts with ticker sectors...")
    sql_alerts = f"""
        SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.target_price, s.stop_loss,
               s.catalyst_type, s.sentiment_score, s.conviction_score, s.created_at,
               si.asset_sub_category
        FROM {schema}.mimir_trade_signals s
        LEFT JOIN LATERAL (
            SELECT asset_sub_category
            FROM {schema}.mimir_sentiment_impacts
            WHERE ticker = s.ticker AND asset_sub_category IS NOT NULL
            LIMIT 1
        ) si ON true
        WHERE s.trigger_price IS NOT NULL AND s.trigger_price > 0
          AND s.created_at >= '2025-01-01'
        ORDER BY s.created_at ASC
    """
    df_alerts = pd.read_sql(sql_alerts, conn)
    print(f"Loaded {len(df_alerts)} historical alerts with sector mappings.")
    
    conn.close()

if __name__ == "__main__":
    run_test()
