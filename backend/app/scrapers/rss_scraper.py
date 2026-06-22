# backend/app/scrapers/rss_scraper.py
import feedparser
import requests
import time
import random
import hashlib
from datetime import datetime
from urllib.parse import urlparse
import psycopg2
from psycopg2.extras import execute_values
from backend.app.database import get_db_connection

# --- RSS FEEDS (same as before) ---
FINANCIAL_RSS_FEEDS = [
    "https://www.bangkokpost.com/rss/data/business.xml",
    "https://thestandard.co/category/wealth/feed/",
    "https://www.kaohoon.com/feed",
    "https://www.moneybuffalo.in.th/feed",
    "https://www.thansettakij.com/rss/feed/money_market",
    "https://www.scb.co.th/en/about-us/news/rss.xml",
    "https://www.dealstreetasia.com/feed",
    "https://finance.yahoo.com/news/rss",
    "http://feeds.reuters.com/reuters/businessNews",
    "http://feeds.reuters.com/reuters/financialsNews",
    "http://feeds.marketwatch.com/marketwatch/topstories",
    "http://feeds.marketwatch.com/marketwatch/marketpulse",
    "https://www.ft.com/?format=rss",
    "https://www.ft.com/companies?format=rss",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069",
    "https://seekingalpha.com/feed.xml",
    "https://www.investing.com/rss/news_285.rss",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://www.economist.com/finance-and-economics/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://asia.nikkei.com/rss/feed/nar",
    "https://asia.nikkei.com/rss/feed/business",
    "https://asia.nikkei.com/rss/feed/markets",
    "https://www.businesstimes.com.sg/rss/top-stories",
    "https://www.businesstimes.com.sg/rss/banking-finance",
    "https://www.chinadaily.com.cn/rss/biz.xml",
    "https://www.scmp.com/rss/92/feed",
    "https://www.scmp.com/rss/318206/feed",
    "https://www.thehindubusinessline.com/?service=rss",
    "https://economictimes.indiatimes.com/rssfeedsdefault.cms",
    "https://www.bloomberg.com/feeds/bproperty.xml",
    "https://www.theguardian.com/business/rss",
    "https://www.cnbc.com/id/19794221/device/rss/rss.xml",
    "https://www.cnbc.com/id/10000115/device/rss/rss.xml",
    "https://www.forbes.com/business/feed/",
    "https://www.forbes.com/money/feed/",
    "https://www.nasdaq.com/feed/rssoutbound?category=Markets"
]

def get_existing_title_hashes():
    """Fetch all title hashes currently in the database."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT title_hash FROM yggdrasil.mimir_raw_articles")
        existing = {row[0] for row in cur.fetchall()}
        cur.close()
        conn.close()
        return existing
    except Exception:
        # Table might not exist yet
        return set()


def fetch_financial_feed_batch(feeds_list=FINANCIAL_RSS_FEEDS):
    """
    Scrape ALL articles from RSS feeds. No filtering.
    Includes batch-level AND database-level dedupe to prevent duplicate inserts.
    """
    normalized_records = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*'
    }

    # --- LOAD EXISTING TITLE HASHES FROM DATABASE ---
    existing_title_hashes = get_existing_title_hashes()
    db_duplicates = 0

    # --- BATCH-LEVEL DEDUPE TRACKING ---
    seen_links = set()
    seen_hashes = set()
    seen_titles = set()
    batch_duplicates = 0

    total_feeds = 0
    total_articles = 0

    print("\n" + "="*60)
    print("📰 MIMIR RSS SCRAPER (No Filter)")
    print(f"   Existing title hashes in DB: {len(existing_title_hashes)}")
    print("="*60)

    for url in feeds_list:
        try:
            domain = urlparse(url).netloc
            total_feeds += 1

            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 429:
                print(f"⚠️ Rate limited on {domain}. Sleeping 5s...")
                time.sleep(5)
                continue
            resp.raise_for_status()
            feed = feedparser.parse(resp.text)

            feed_total = 0
            feed_duplicates = 0

            for entry in feed.entries:
                feed_total += 1
                total_articles += 1

                title = entry.get('title', '').strip()
                summary = entry.get('summary', '').strip()
                link = entry.get('link')

                if not link:
                    continue

                # --- GENERATE HASHES ---
                url_hash = hashlib.md5(link.strip().encode()).hexdigest()
                title_hash = hashlib.md5(title.lower().strip().encode()).hexdigest()

                # --- 1. CHECK DUPLICATE LINK IN BATCH ---
                if link in seen_links:
                    feed_duplicates += 1
                    batch_duplicates += 1
                    continue
                seen_links.add(link)

                # --- 2. CHECK DUPLICATE HASH IN BATCH ---
                if url_hash in seen_hashes:
                    feed_duplicates += 1
                    batch_duplicates += 1
                    continue
                seen_hashes.add(url_hash)

                # --- 3. CHECK DUPLICATE TITLE IN BATCH (same run) ---
                title_key = title[:60].lower().strip()
                if title_key in seen_titles:
                    feed_duplicates += 1
                    batch_duplicates += 1
                    continue
                seen_titles.add(title_key)

                # --- 4. CHECK DUPLICATE TITLE IN DATABASE (previous runs) ---
                if title_hash in existing_title_hashes:
                    feed_duplicates += 1
                    batch_duplicates += 1
                    db_duplicates += 1
                    print(f"   🔄 DB duplicate title: {title[:60]}...")
                    continue
                existing_title_hashes.add(title_hash)  # Add to set for this run

                # --- CLEAN TIMESTAMP ---
                pub_ts = None
                if entry.get('published_parsed'):
                    try:
                        dt = datetime.fromtimestamp(time.mktime(entry['published_parsed']))
                        pub_ts = dt.isoformat()
                    except:
                        pass

                record = {
                    "source_name": domain,
                    "feed_url": url,
                    "title": title,
                    "link": link.strip(),
                    "published_raw": entry.get('published'),
                    "published_ts": pub_ts,
                    "summary": summary,
                    "url_hash": url_hash,
                    "title_hash": title_hash  # <-- ADD THIS
                }
                normalized_records.append(record)

            print(f"\n📡 {domain} → {feed_total} articles (duplicates skipped: {feed_duplicates})")

            time.sleep(random.uniform(1.0, 2.5))

        except Exception as e:
            print(f"❌ Error scraping {url}: {e}")
            continue

    print("\n" + "="*60)
    print("📊 SCRAPE SUMMARY")
    print("="*60)
    print(f"Feeds processed:           {total_feeds}")
    print(f"Articles scraped:          {total_articles}")
    print(f"Duplicates skipped (batch): {batch_duplicates}")
    print(f"Duplicates skipped (DB):    {db_duplicates}")
    print(f"Unique articles:           {len(normalized_records)}")
    print("="*60)

    return normalized_records