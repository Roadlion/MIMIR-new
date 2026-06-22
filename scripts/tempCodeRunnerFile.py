#on foenem grave
import os
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.scrapers.rss_scraper import fetch_financial_feed_batch

dotenv_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path)

# --- CONNECTION STRING (pantheon_db is in DB_NAME) ---
DB_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
print(DB_URL)

# --- TABLE SCHEMA (explicitly yggdrasil schema) ---
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
    scraped_at TIMESTAMPTZ DEFAULT NOW()
);
"""

def insert_articles(records):
    if not records:
        print("No fuckin' records. Exit.")
        return

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    # Ensure table exists in the right schema
    cur.execute(CREATE_TABLE_SQL)

    # Bulk insert with ON CONFLICT on url_hash
    sql = """
    INSERT INTO yggdrasil.mimir_raw_articles (
        source_name, feed_url, title, link, 
        published_raw, published_ts, summary, url_hash
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
        r['url_hash']
    ) for r in records]

    execute_values(cur, sql, data)
    conn.commit()

    print(f"Inserted {cur.rowcount} new rows. Skipped duplicates.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    print("Scrapin' yer shite...")
    records = fetch_financial_feed_batch()
    print(f"Got {len(records)} articles. Shovin' em in DB...")
    insert_articles(records)
    print("Done. Go drink.")