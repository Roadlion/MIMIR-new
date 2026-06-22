# scripts/test_sentiment_pipeline.py
# Test run: process 5 unscored articles, skip duplicates

import os
import sys
import json
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from pathlib import Path
import time

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.sentiment.deepseek_client import DeepSeekSentiment
from backend.app.sentiment.asset_mapper import resolve_ticker, resolve_country_code, resolve_region
from backend.app.database import get_db_connection

# Load .env
dotenv_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path)

# --- Configuration ---
BATCH_SIZE = 5  # Just 5 for test run
MAX_RETRIES_PER_ARTICLE = 2

def get_unscored_articles(limit=BATCH_SIZE):
    """Fetch articles that haven't been analyzed yet."""
    conn = get_db_connection()
    cur = conn.cursor()
    
    cur.execute("""
        SELECT id, title, summary 
        FROM yggdrasil.mimir_raw_articles 
        WHERE id NOT IN (
            SELECT DISTINCT article_id 
            FROM yggdrasil.mimir_sentiment_impacts
        )
        ORDER BY published_ts DESC NULLS LAST
        LIMIT %s
    """, (limit,))
    
    articles = cur.fetchall()
    cur.close()
    conn.close()
    return articles

def resolve_asset_details(asset):
    """Resolve ticker, country, region for an asset."""
    asset_name = asset.get("asset_name", "").strip()
    
    ticker, found = resolve_ticker(asset_name)
    if not found:
        print(f"[WARN] Unknown asset: '{asset_name}' - add to asset_mapper.py")
    
    country = asset.get("country")
    if country:
        country = resolve_country_code(country) or country
    else:
        country = resolve_country_code(asset_name)
    
    region = asset.get("region")
    if not region and country:
        region = resolve_region(country)
    
    return ticker, country, region

def process_single_article(client, article_id, title, summary):
    """Call DeepSeek, parse JSON, map tickers, prepare insert data."""
    for attempt in range(MAX_RETRIES_PER_ARTICLE):
        try:
            response = client.score_article_with_assets(title, summary or "")
            break
        except Exception as e:
            print(f"  ⚠️ Attempt {attempt+1} failed: {e}")
            if attempt == MAX_RETRIES_PER_ARTICLE - 1:
                print(f"  ❌ Giving up on article {article_id}")
                return []
            time.sleep(3)
            continue
    
    if not response or "assets" not in response:
        print(f"[WARN] No assets found for article {article_id}")
        return []
    
    impacts = []
    for asset in response.get("assets", []):
        asset_name = asset.get("asset_name", "").strip()
        if not asset_name:
            continue
        
        ticker, country, region = resolve_asset_details(asset)
        
        impact = (
            article_id,
            asset_name,
            asset.get("asset_category", "UNKNOWN"),
            asset.get("sub_category", None),
            country,
            region,
            asset.get("sentiment_score", 0.0),
            asset.get("confidence", 0.5),
            asset.get("direction", "neutral"),
            asset.get("magnitude", "MEDIUM"),
            asset.get("reasoning", ""),
            ticker,
            asset.get("policy_signal")
        )
        impacts.append(impact)
    
    return impacts

def insert_impacts(impacts):
    """Bulk insert sentiment impacts."""
    if not impacts:
        return 0
    
    conn = get_db_connection()
    cur = conn.cursor()
    
    sql = """
    INSERT INTO yggdrasil.mimir_sentiment_impacts (
        article_id,
        asset_name,
        asset_category,
        asset_sub_category,
        country,
        region,
        sentiment_score,
        confidence,
        direction,
        magnitude,
        reasoning,
        ticker,
        policy_signal
    ) VALUES %s
    ON CONFLICT (article_id, asset_name) DO NOTHING;
    """
    
    execute_values(cur, sql, impacts)
    conn.commit()
    
    inserted = cur.rowcount
    cur.close()
    conn.close()
    return inserted

def refresh_aggregates():
    """Refresh the materialized view."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT refresh_mimir_aggregates();")
        conn.commit()
        cur.close()
        conn.close()
        print("🔄 Materialized view refreshed.")
    except Exception as e:
        print(f"⚠️ Failed to refresh view: {e}")

def main():
    print("🧪 MIMIR Sentiment Pipeline - Test Run")
    print(f"Processing up to {BATCH_SIZE} articles...")
    
    articles = get_unscored_articles()
    if not articles:
        print("✅ No unscored articles. You're all caught up.")
        return
    
    print(f"Found {len(articles)} unscored articles.")
    
    client = DeepSeekSentiment()
    total_impacts = 0
    
    for idx, (article_id, title, summary) in enumerate(articles, 1):
        print(f"\n[{idx}/{len(articles)}] Scoring: {title[:60]}...")
        
        try:
            impacts = process_single_article(client, article_id, title, summary)
            if impacts:
                inserted = insert_impacts(impacts)
                total_impacts += inserted
                print(f"  ✅ Inserted {inserted} assets for article {article_id}")
            else:
                print(f"  ⏭️ No assets extracted, skipping article {article_id}")
        except Exception as e:
            print(f"  ❌ Error processing article {article_id}: {e}")
    
    print(f"\n✅ Done. Inserted {total_impacts} asset sentiments.")
    
    if total_impacts > 0:
        print("🔄 Refreshing materialized view...")
        refresh_aggregates()

if __name__ == "__main__":
    main()