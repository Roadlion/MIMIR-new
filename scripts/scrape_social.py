# scripts/scrape_social.py
import sys
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

import os
import re
import html
import time
import random
import hashlib
import urllib3
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.database import get_db_connection
from backend.app.sentiment.deepseek_client import DeepSeekSentiment
from backend.app.sentiment.asset_mapper import ASSET_TO_TICKER, resolve_ticker

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REDDIT_FEEDS = {
    "r/stocks": "https://www.reddit.com/r/stocks/.rss",
    "r/wallstreetbets": "https://www.reddit.com/r/wallstreetbets/.rss",
    "r/macroeconomics": "https://www.reddit.com/r/macroeconomics/.rss",
    "r/commodities": "https://www.reddit.com/r/commodities/.rss",
    "r/CryptoCurrency": "https://www.reddit.com/r/CryptoCurrency/.rss"
}

# Common words to ignore when scanning uppercase words for tickers
IGNORE_TICKERS = {
    "I", "A", "AND", "THE", "FOR", "CEO", "CFO", "SEC", "FED", "USA", "GDP", "CPI", 
    "ETF", "DD", "ATH", "YOLO", "FOMO", "FUD", "WSB", "ATH", "PE", "EPS", "AI", 
    "DIY", "IRS", "FAQ", "PDF", "URL", "API", "IPO", "NYSE", "AMEX", "REIT", "PBR",
    "USD", "EUR", "JPY", "GBP", "CAD", "AUD", "CHF", "CNY", "THB", "BTC", "ETH"
}

def clean_html(raw_html):
    """Strip HTML tags and unescape HTML entities."""
    if not raw_html:
        return ""
    # Strip HTML tags
    clean_text = re.sub(r'<[^>]*>', ' ', raw_html)
    # Unescape HTML characters (e.g. &amp; -> &)
    clean_text = html.unescape(clean_text)
    # Replace multiple spaces/newlines
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    return clean_text

def get_known_tickers():
    """Load known tickers from asset_mapper and database dynamic tickers."""
    tickers = {}
    
    # 1. Load from static ASSET_TO_TICKER
    for name, ticker in ASSET_TO_TICKER.items():
        clean_ticker = ticker.split("=")[0].split("-")[0].strip().upper()
        if len(clean_ticker) >= 2 and clean_ticker not in IGNORE_TICKERS:
            tickers[clean_ticker] = (ticker, name)
            
    # 2. Load from database dynamic/sentiment tickers
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT ticker, asset_name FROM yggdrasil.mimir_sentiment_impacts WHERE ticker IS NOT NULL")
        for row in cur.fetchall():
            clean_ticker = row[0].split("=")[0].split("-")[0].strip().upper()
            if len(clean_ticker) >= 2 and clean_ticker not in IGNORE_TICKERS:
                tickers[clean_ticker] = (row[0], row[1])
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Note: Could not query database for tickers: {e}")
        
    return tickers

def detect_assets(title, summary, known_tickers):
    """Scan text for cash tags, uppercase tickers, and commodity keywords."""
    detected = {}  # ticker -> asset_name
    text = f" {title} {summary} "
    
    # 1. Scan for cashtags like $TSLA or $BTC
    cashtags = re.findall(r'\$([A-Z]{2,5})\b', text)
    for tag in cashtags:
        tag = tag.upper()
        if tag not in IGNORE_TICKERS:
            # Resolve to full ticker if we can, else default
            resolved, found = resolve_ticker(tag)
            detected[resolved if found else tag] = tag
            
    # 2. Scan for raw uppercase ticker words
    words = re.findall(r'\b([A-Z]{2,5})\b', text)
    for w in words:
        w = w.upper()
        if w in known_tickers:
            ticker, name = known_tickers[w]
            detected[ticker] = name
            
    # 3. Scan for commodity keywords
    lower_text = text.lower()
    commodity_keywords = {
        "gold": ("GC=F", "GOLD"),
        "silver": ("SI=F", "SILVER"),
        "crude oil": ("CL=F", "CRUDE OIL"),
        "brent": ("BZ=F", "BRENT CRUDE"),
        "natural gas": ("NG=F", "NATURAL GAS"),
        "copper": ("COPX", "COPPER"),
        "wheat": ("WEAT", "WHEAT"),
        "corn": ("CORN", "CORN"),
        "soybeans": ("SOYB", "SOYBEANS"),
        "dry bulk": ("BDRY", "DRY BULK"),
        "shipping": ("BDRY", "SHIPPING")
    }
    
    for kw, (ticker, name) in commodity_keywords.items():
        if re.search(r'\b' + re.escape(kw) + r'\b', lower_text):
            detected[ticker] = name
            
    return detected

def scrape_feeds():
    """Scrape Reddit feeds and return list of parsed posts."""
    from curl_cffi.requests import Session
    
    posts = []
    seen_links = set()
    
    print("\n" + "="*60)
    print("🤖 MIMIR SOCIAL SCRAPER (Reddit RSS)")
    print("="*60)
    
    # Create an impersonated Chrome session to bypass Reddit's aggressive 429 rate limiting
    sess = Session(impersonate="chrome")
    sess.verify = False
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    })
    
    for channel, url in REDDIT_FEEDS.items():
        max_retries = 3
        backoff = 10.0
        
        for attempt in range(max_retries):
            try:
                print(f"Fetching {channel} RSS (attempt {attempt+1}/{max_retries})...")
                resp = sess.get(url, timeout=10)
                
                if resp.status_code == 429:
                    delay = backoff * (2.5 ** attempt) + random.uniform(2.0, 5.0)
                    print(f"⚠️ Rate limited (429) on {channel}. Backing off for {delay:.1f}s...")
                    time.sleep(delay)
                    continue
                    
                if resp.status_code != 200:
                    print(f"⚠️ Failed to fetch {channel}: HTTP {resp.status_code}")
                    break
                    
                feed = feedparser.parse(resp.text)
                feed_posts = 0
                
                for entry in feed.entries:
                    link = entry.get('link', '').strip()
                    if not link or link in seen_links:
                        continue
                    seen_links.add(link)
                    
                    title = clean_html(entry.get('title', ''))
                    summary_html = entry.get('summary', '')
                    summary = clean_html(summary_html)
                    
                    # Strip out standard reddit RSS footer links
                    summary = re.sub(r'submitted by.*', '', summary).strip()
                    
                    # Parse timestamp
                    published_ts = datetime.now(timezone.utc)  # fallback
                    updated_raw = entry.get('updated')
                    if updated_raw:
                        try:
                            # Parse ISO 8601 (Reddit uses +00:00)
                            dt = datetime.fromisoformat(updated_raw)
                            published_ts = dt.astimezone(timezone.utc)
                        except Exception:
                            pass
                    
                    posts.append({
                        "platform": "reddit",
                        "channel": channel,
                        "title": title,
                        "summary": summary,
                        "link": link,
                        "published_ts": published_ts
                    })
                    feed_posts += 1
                    
                print(f"📡 {channel} → Scraped {feed_posts} posts")
                break  # Success, exit retry loop
                
            except Exception as e:
                print(f"❌ Error scraping {channel}: {e}")
                time.sleep(2)
                
        # Sleep between different feeds to be polite
        time.sleep(8.0 + random.uniform(2.0, 5.0))
            
    print(f"\nTotal posts scraped: {len(posts)}")
    return posts

def main():
    # 1. Fetch posts
    posts = scrape_feeds()
    if not posts:
        print("No social chatter found. Exiting.")
        return
        
    # 2. Get known tickers
    known_tickers = get_known_tickers()
    print(f"Loaded {len(known_tickers)} known tickers for scanning.")
    
    # 3. Detect assets and group posts
    grouped_posts = {}  # (ticker, asset_name) -> list of posts
    for p in posts:
        detected = detect_assets(p["title"], p["summary"], known_tickers)
        for ticker, asset_name in detected.items():
            key = (ticker, asset_name)
            if key not in grouped_posts:
                grouped_posts[key] = []
            grouped_posts[key].append(p)
            
    print(f"\nGrouped chatter across {len(grouped_posts)} active assets:")
    for (ticker, name), plist in grouped_posts.items():
        print(f" - {ticker} ({name}): {len(plist)} posts")
        
    if not grouped_posts:
        print("No asset mentions detected in the current social batch. Done.")
        return
        
    # 4. Score consolidated chatter using DeepSeek
    client = DeepSeekSentiment()
    conn = get_db_connection()
    cur = conn.cursor()
    
    scored_count = 0
    upsert_sql = """
    INSERT INTO yggdrasil.mimir_social_chatter (
        platform, channel, ticker, asset_name, bucket_ts, 
        sentiment_score, confidence, post_count, engagement_score, summary_text
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (platform, channel, ticker, bucket_ts) DO UPDATE 
    SET sentiment_score = EXCLUDED.sentiment_score,
        confidence = EXCLUDED.confidence,
        post_count = EXCLUDED.post_count,
        engagement_score = EXCLUDED.engagement_score,
        summary_text = EXCLUDED.summary_text,
        scraped_at = NOW();
    """
    
    for (ticker, asset_name), plist in grouped_posts.items():
        # Group by subreddit/channel and hourly bucket
        # (This handles the case where the same ticker is mentioned across different subreddits)
        subgroups = {} # (channel, bucket_ts) -> list of posts
        for p in plist:
            # Round down to the beginning of the hour for bucket_ts
            ts = p["published_ts"]
            bucket_ts = datetime(ts.year, ts.month, ts.day, ts.hour, 0, 0, tzinfo=timezone.utc)
            skey = (p["channel"], bucket_ts)
            if skey not in subgroups:
                subgroups[skey] = []
            subgroups[skey].append(p)
            
        for (channel, bucket_ts), subposts in subgroups.items():
            print(f"\nScoring {ticker} in {channel} for hour {bucket_ts.isoformat()} ({len(subposts)} posts)...")
            
            # Consolidate post contents
            consolidated = []
            for sp in subposts:
                link_line = f"\nLink: {sp['link']}" if sp.get('link') else ""
                consolidated.append(f"Title: {sp['title']}{link_line}\nContent: {sp['summary']}")
            summary_text = "\n\n---\n\n".join(consolidated)[:3000] # Cap size for API efficiency
            
            # Call DeepSeek with strict relevance scoring
            try:
                title_str = f"Social chatter aggregate for ticker {ticker} in {channel}"
                result = client.score_article_with_assets(
                    title=title_str, 
                    summary=summary_text,
                    force_relevance=True
                )
                
                # Extract sentiment for this specific asset from DeepSeek response list
                # (DeepSeek returns a list of assets, we want to match our ticker or take overall sentiment)
                sentiment_score = 0.0
                confidence = 0.8
                matched = False
                
                for asset in result.get("assets", []):
                    # Check if ticker matches or asset name matches
                    asset_ticker = asset.get("ticker", "")
                    if asset_ticker and asset_ticker.strip().upper() == ticker.upper():
                        sentiment_score = float(asset.get("sentiment_score", 0.0))
                        confidence = float(asset.get("confidence", 0.8))
                        matched = True
                        break
                        
                if not matched and result.get("assets"):
                    # Fallback to the first parsed asset sentiment if any
                    first_asset = result["assets"][0]
                    sentiment_score = float(first_asset.get("sentiment_score", 0.0))
                    confidence = float(first_asset.get("confidence", 0.8))
                elif not matched:
                    # Fallback to overall sentiment
                    sentiment_score = float(result.get("overall_sentiment", 0.0))
                    
                # Store aggregated metrics
                # RSS has no raw engagement field, default each post to an engagement weight of 10
                engagement_score = len(subposts) * 10
                
                cur.execute(upsert_sql, (
                    "reddit",
                    channel,
                    ticker,
                    asset_name,
                    bucket_ts,
                    sentiment_score,
                    confidence,
                    len(subposts),
                    engagement_score,
                    summary_text[:1000] # Store preview in DB
                ))
                conn.commit()
                scored_count += 1
                print(f"✅ Saved to DB: {ticker} | Sentiment: {sentiment_score:.2f} | Conf: {confidence:.2f}")
                
            except Exception as ex:
                print(f"❌ Error scoring consolidated chatter for {ticker}: {ex}")
                
    cur.close()
    conn.close()
    print(f"\nScraped and updated {scored_count} social sentiment aggregates in DB.")

if __name__ == "__main__":
    main()
