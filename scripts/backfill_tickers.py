# scripts/backfill_tickers.py
import sys
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

import os
from pathlib import Path

# Add project root to path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.sentiment.asset_mapper import resolve_ticker
from backend.app.routers.prices import fetch_and_cache_ticker

def backfill_tickers():
    print("="*60)
    print("🔍 MIMIR TICKER BACKFILLING UTILITY")
    print("="*60)
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    # 1. Find all assets that have NULL or empty ticker values
    cur.execute("""
        SELECT DISTINCT asset_name 
        FROM yggdrasil.mimir_sentiment_impacts 
        WHERE ticker IS NULL OR ticker = ''
    """)
    rows = cur.fetchall()
    missing_assets = [row[0] for row in rows]
    
    print(f"Found {len(missing_assets)} unique assets in database missing tickers.")
    
    resolved_count = 0
    resolved_tickers = set()
    
    for asset in missing_assets:
        ticker, found = resolve_ticker(asset)
        if found and ticker:
            print(f"  ✅ Resolved '{asset}' -> {ticker}")
            cur.execute("""
                UPDATE yggdrasil.mimir_sentiment_impacts
                SET ticker = %s
                WHERE asset_name = %s
            """, (ticker, asset))
            resolved_count += 1
            resolved_tickers.add(ticker)
        else:
            print(f"  ❌ Could not resolve '{asset}'")
            
    conn.commit()
    print(f"\nSuccessfully backfilled {resolved_count} assets with tickers in sentiment impacts table.")
    
    # 2. Fetch and cache prices for all resolved tickers
    if resolved_tickers:
        print("\n" + "="*60)
        print(f"📈 BACKFILLING PRICE DATA FOR {len(resolved_tickers)} TICKERS")
        print("="*60)
        for i, ticker in enumerate(sorted(resolved_tickers)):
            print(f"Fetching and caching price data for {ticker} ({i+1}/{len(resolved_tickers)})...")
            try:
                fetch_and_cache_ticker(ticker, conn)
            except Exception as pe:
                print(f"  Error caching price for {ticker}: {pe}")
                
    cur.close()
    conn.close()
    print("\nBackfill and price caching complete!")

if __name__ == '__main__':
    backfill_tickers()
