# scripts/push_to_db.py
# Fully fixed: includes title_hash dedupe to prevent duplicate headlines

import sys
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

import os
import hashlib
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from pathlib import Path

# Add project root to path so we can import the scraper
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.scrapers.rss_scraper import fetch_financial_feed_batch
from backend.app.scrapers.newsapi_scraper import fetch_newsapi_articles, fetch_gnews_articles

# Load .env
dotenv_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path)

# --- CONNECTION STRING ---
DB_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
print(f"Connecting to {os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")

# --- TABLE SCHEMA (with title_hash) ---
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS yggdrasil.mimir_raw_articles (
    id SERIAL PRIMARY KEY,
    source_name TEXT,
    feed_url TEXT,
    title TEXT,
    link TEXT UNIQUE,
    published_raw TEXT,
    published_ts TIMESTAMPTZ,
    summary TEXT,
    url_hash TEXT UNIQUE,
    title_hash VARCHAR(32) UNIQUE,   -- <-- NEW column for dedupe
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    scoring_status VARCHAR(20) DEFAULT 'triage_pending'
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_raw_articles_published_ts ON yggdrasil.mimir_raw_articles (published_ts DESC);
CREATE INDEX IF NOT EXISTS idx_raw_articles_source ON yggdrasil.mimir_raw_articles (source_name);
CREATE INDEX IF NOT EXISTS idx_raw_articles_hash ON yggdrasil.mimir_raw_articles (url_hash);
CREATE INDEX IF NOT EXISTS idx_raw_articles_title_hash ON yggdrasil.mimir_raw_articles (title_hash);
CREATE INDEX IF NOT EXISTS idx_raw_articles_scoring_status ON yggdrasil.mimir_raw_articles (scoring_status);
"""

def insert_articles(records):
    """
    Insert records, skipping duplicates based on url_hash AND title_hash.
    """
    if not records:
        print("No fuckin' records. Exit.")
        return

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # Ensure table exists with title_hash
    cur.execute(CREATE_TABLE_SQL)

    # Ensure title_hash column exists (in case table existed without it)
    cur.execute("""
        ALTER TABLE yggdrasil.mimir_raw_articles 
        ADD COLUMN IF NOT EXISTS title_hash VARCHAR(32) UNIQUE;
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_raw_articles_title_hash 
        ON yggdrasil.mimir_raw_articles (title_hash);
    """)

    # Pre-calculate hashes and ensure url_hash is set
    for r in records:
        title_hash = hashlib.md5(r['title'].lower().strip().encode()).hexdigest()
        r['title_hash'] = title_hash
        if not r.get('url_hash'):
            r['url_hash'] = hashlib.md5(r['link'].strip().encode()).hexdigest()

    # --- Get existing title hashes from DB for this batch ONLY ---
    batch_hashes = list({r['title_hash'] for r in records if r.get('title_hash')})
    existing_hashes = set()
    if batch_hashes:
        cur.execute("SELECT title_hash FROM yggdrasil.mimir_raw_articles WHERE title_hash = ANY(%s)", (batch_hashes,))
        existing_hashes = {row[0] for row in cur.fetchall()}

    # --- Filter out duplicates ---
    unique_records = []
    skipped_count = 0
    for r in records:
        title_hash = r['title_hash']
        # Check if we've already seen this title in this run or in DB
        if title_hash in existing_hashes:
            skipped_count += 1
            continue
        existing_hashes.add(title_hash)  # avoid duplicates within the same batch
        unique_records.append(r)

    if not unique_records:
        print(f"All {len(records)} articles are duplicates (by title). Skipping.")
        cur.close()
        conn.close()
        return

    # --- Bulk insert with ON CONFLICT (url_hash) ---
    sql = """
    INSERT INTO yggdrasil.mimir_raw_articles (
        source_name, feed_url, title, link, 
        published_raw, published_ts, summary, url_hash, title_hash
    ) VALUES %s
    ON CONFLICT (url_hash) DO NOTHING;
    """

    data = [(
        r['source_name'],
        r['feed_url'],
        r['title'],
        r['link'],
        r['published_raw'],
        r['published_ts'],
        r['summary'],
        r['url_hash'],
        r['title_hash']
    ) for r in unique_records]

    try:
        execute_values(cur, sql, data)
        conn.commit()
        inserted = cur.rowcount
        print(f"Inserted {inserted} new rows. Skipped {len(records) - inserted - skipped_count} duplicates (URL or title).")
    except Exception as e:
        print(f"❌ Insert failed: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    print("Scrapin' yer shite...")
    records = fetch_financial_feed_batch()
    
    try:
        newsapi_records = fetch_newsapi_articles()
        records.extend(newsapi_records)
    except Exception as ne:
        print(f"⚠️ Failed to fetch from NewsAPI: {ne}")
        
    try:
        gnews_records = fetch_gnews_articles()
        records.extend(gnews_records)
    except Exception as ge:
        print(f"⚠️ Failed to fetch from GNews: {ge}")
        
    print(f"Got {len(records)} total articles. Shovin' em in DB...")
    insert_articles(records)
    print("Done. Go drink.")