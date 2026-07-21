# scripts/scrape_twitter.py
import os
import sys
import re
import time
import random
import json
import urllib3
from datetime import datetime, timezone
from pathlib import Path
import requests

# Suppress insecure request warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.database import get_db_connection
from backend.app.config import get_settings
from backend.app.sentiment.deepseek_client import DeepSeekSentiment
from backend.app.routers.prices import DEFAULT_TICKERS

settings = get_settings()

# Curated financial handles (Expanded to increase global macro & stock volume)
TWITTER_HANDLES = [
    "unusual_whales", "zerohedge", "KobeissiLetter", "charliebilello", "SvenHenrich",
    "federalreserve", "DeItaone", "SquawkCNBC", "YahooFinance", "MarketWatch",
    "bespokeinvest", "Fxhedgers", "Tier10k", "Stocktwits", "SubstackInc",
    "CNBC", "WSJmarkets", "BloombergTV", "FinancialTimes", "ReutersBiz",
    "jimcramer", "LizAnnSonders", "elerianm", "Schuldensuehner", "NorthmanTrader",
    "NateGeraci", "EricBalchunas", "CiovaccoCapital", "MacroAlf", "AndreasSteno",
    "LynAldenContact", "Gurgavin", "OptionsAction", "OptionsHawk", "SpotGamma"
]

def clean_html(text):
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    return text.strip()

def is_high_quality_post(text):
    """Heuristic filter to drop homework questions, non-market noise, and short memes."""
    text_lower = text.lower()
    
    # 1. Length constraint (too short = likely a meme or just a link)
    words = text_lower.split()
    if len(words) < 5:
        return False
        
    # 2. Blacklist constraint
    blacklist = [
        "homework", "help me with", "assignment", "essay", "noob", 
        "dumb question", "school", "project for", "explain like i'm", 
        "exam", "quiz", "class", "my professor", "textbook"
    ]
    for bad_phrase in blacklist:
        if bad_phrase in text_lower:
            return False
            
    # 3. Excessive Emoji spam (basic check)
    emoji_count = len(re.findall(r'[^\w\s,\.\?!\-\'\"\$\%\(\)\[\]\{\}\:\;\#\@]', text_lower))
    if emoji_count > 15:
        return False
        
    return True

def get_known_tickers():
    conn = get_db_connection()
    cur = conn.cursor()
    tickers = {}
    for t in DEFAULT_TICKERS:
        clean = t.strip().lstrip('$').upper()
        tickers[clean] = (clean, clean)
    try:
        cur.execute(f"SELECT ticker, asset_name FROM {settings.mimir_schema}.mimir_dynamic_tickers WHERE ticker IS NOT NULL")
        for row in cur.fetchall():
            clean = row[0].strip().upper()
            tickers[clean] = (clean, row[1].strip())
    except Exception as e:
        print(f"[TWITTER] Error fetching dynamic tickers: {e}")
    finally:
        cur.close()
        conn.close()
    return tickers

def detect_assets(text, known_tickers):
    detected = {}
    words = re.findall(r'\b\$?[A-Z]{1,5}\b', text)
    for w in words:
        w = w.upper().replace('$', '')
        if w in known_tickers:
            ticker, name = known_tickers[w]
            detected[ticker] = name
            
    lower_text = text.lower()
    commodity_keywords = {
        "gold": ("GC=F", "GOLD"),
        "silver": ("SI=F", "SILVER"),
        "crude oil": ("CL=F", "CRUDE OIL"),
        "brent": ("BZ=F", "BRENT CRUDE"),
        "natural gas": ("NG=F", "NATURAL GAS"),
        "copper": ("COPX", "COPPER"),
        "wheat": ("WEAT", "WHEAT")
    }
    for kw, (ticker, name) in commodity_keywords.items():
        if re.search(r'\b' + re.escape(kw) + r'\b', lower_text):
            detected[ticker] = name
    return detected

def scrape_tweets():
    posts = []
    seen_links = set()
    
    print("\n" + "="*60)
    print("[TWITTER] MIMIR TARGETED TWITTER SCRAPER (Syndication CDN)")
    print("="*60)
    
    sess = requests.Session()
    sess.verify = False
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    })
    
    for handle in TWITTER_HANDLES:
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
        try:
            print(f"[TWITTER] Scraping @{handle} from Syndication CDN...")
            resp = sess.get(url, timeout=12)
            if resp.status_code != 200:
                print(f" [!] CDN returned HTTP {resp.status_code} for @{handle}")
                continue
                
            match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text)
            if not match:
                print(f" [!] Could not find embedded JSON payload for @{handle}")
                continue
                
            data = json.loads(match.group(1))
            props = data.get("props", {})
            pageProps = props.get("pageProps", {})
            timeline = pageProps.get("timeline", {})
            entries = timeline.get("entries", [])
            
            feed_posts = 0
            for entry in entries:
                tweet = entry.get("content", {}).get("tweet", {})
                if not tweet:
                    continue
                    
                tweet_id = tweet.get("id_str")
                link = f"https://twitter.com/{handle}/status/{tweet_id}"
                if not link or link in seen_links:
                    continue
                seen_links.add(link)
                
                text = clean_html(tweet.get("text", ""))
                if not text:
                    continue
                    
                if not is_high_quality_post(text):
                    continue
                    
                # Parse Twitter timestamp: "Thu Jul 02 12:30:00 +0000 2026"
                published_ts = datetime.now(timezone.utc)
                created_at_raw = tweet.get("created_at")
                if created_at_raw:
                    try:
                        dt = datetime.strptime(created_at_raw, "%a %b %d %H:%M:%S %z %Y")
                        published_ts = dt.astimezone(timezone.utc)
                    except Exception:
                        pass
                        
                likes = int(tweet.get("favorite_count", 0))
                retweets = int(tweet.get("retweet_count", 0))
                engagement = likes + (retweets * 3)
                
                posts.append({
                    "platform": "twitter",
                    "channel": handle,
                    "title": text[:80] + "..." if len(text) > 80 else text,
                    "summary": text,
                    "link": link,
                    "published_ts": published_ts,
                    "engagement_score": engagement
                })
                feed_posts += 1
                
            print(f" [ok] @{handle} -> Scraped {feed_posts} tweets successfully.")
        except Exception as e:
            print(f" [error] Error scraping @{handle}: {e}")
            
        time.sleep(2.0 + random.uniform(1.0, 3.0))
        
    print(f"\nTotal tweets scraped: {len(posts)}")
    return posts

def main():
    posts = scrape_tweets()
    if not posts:
        print("No new tweets found. Exiting.")
        return
        
    known_tickers = get_known_tickers()
    print(f"Loaded {len(known_tickers)} known tickers for scanning.")
    
    grouped_posts = {}
    for p in posts:
        detected = detect_assets(p["title"] + " " + p["summary"], known_tickers)
        for ticker, asset_name in detected.items():
            key = (ticker, asset_name)
            if key not in grouped_posts:
                grouped_posts[key] = []
            grouped_posts[key].append(p)
            
    if not grouped_posts:
        print("No relevant asset mentions detected in tweets. Done.")
        return
        
    client = DeepSeekSentiment()
    conn = get_db_connection()
    cur = conn.cursor()
    
    try:
        cur.execute(f"ALTER TABLE {settings.mimir_schema}.mimir_social_chatter ADD COLUMN IF NOT EXISTS content_hash VARCHAR(32)")
        conn.commit()
    except Exception as db_err:
        print(f"Note: Could not run ALTER TABLE: {db_err}")

    # Gather existing content hashes
    existing_hashes = {}
    all_bucket_times = []
    for p in posts:
        ts = p["published_ts"]
        bucket_ts = datetime(ts.year, ts.month, ts.day, ts.hour, 0, 0, tzinfo=timezone.utc)
        all_bucket_times.append(bucket_ts)
        
    if all_bucket_times:
        min_ts = min(all_bucket_times)
        try:
            cur.execute(f"""
                SELECT channel, ticker, bucket_ts, content_hash 
                FROM {settings.mimir_schema}.mimir_social_chatter 
                WHERE platform = 'twitter' AND bucket_ts >= %s
            """, (min_ts,))
            for row in cur.fetchall():
                existing_hashes[(row[0], row[1], row[2])] = row[3]
        except Exception as e:
            print(f"Note: Could not fetch existing hashes: {e}")
            
    scored_count = 0
    upsert_sql = f"""
    INSERT INTO {settings.mimir_schema}.mimir_social_chatter (
        platform, channel, ticker, asset_name, bucket_ts, 
        sentiment_score, confidence, post_count, engagement_score, summary_text, content_hash
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (platform, channel, ticker, bucket_ts) DO UPDATE 
    SET sentiment_score = EXCLUDED.sentiment_score,
        confidence = EXCLUDED.confidence,
        post_count = EXCLUDED.post_count,
        engagement_score = EXCLUDED.engagement_score,
        summary_text = EXCLUDED.summary_text,
        content_hash = EXCLUDED.content_hash,
        scraped_at = NOW();
    """
    
    import hashlib
    for (ticker, asset_name), plist in grouped_posts.items():
        subgroups = {}
        for p in plist:
            ts = p["published_ts"]
            bucket_ts = datetime(ts.year, ts.month, ts.day, ts.hour, 0, 0, tzinfo=timezone.utc)
            skey = (p["channel"], bucket_ts)
            if skey not in subgroups:
                subgroups[skey] = []
            subgroups[skey].append(p)
            
        for (channel, bucket_ts), subposts in subgroups.items():
            # Calculate content hash
            links = sorted([sp['link'] for sp in subposts if sp.get('link')])
            if not links:
                links = sorted([sp['summary'] for sp in subposts])
            content_str = "|".join(links)
            content_hash = hashlib.md5(content_str.encode('utf-8')).hexdigest()
            
            # Check if unchanged
            if existing_hashes.get((channel, ticker, bucket_ts)) == content_hash:
                print(f" ⏭️ {ticker} in @{channel} at {bucket_ts.isoformat()} is unchanged (hash matches). Skipping LLM scoring.")
                continue

            print(f"\nScoring {ticker} in @{channel} for hour {bucket_ts.isoformat()} ({len(subposts)} tweets)...")
            
            consolidated = []
            total_engagement = 0
            for sp in subposts:
                consolidated.append(f"Tweet: {sp['summary']}")
                total_engagement += sp.get("engagement_score", 0)
                
            summary_text = "\n\n---\n\n".join(consolidated)[:3000]
            
            try:
                title_str = f"FinTwit chatter aggregate for @{channel} targeting {ticker}"
                result = client.score_article_with_assets(
                    title=title_str,
                    summary=summary_text,
                    force_relevance=True
                )
                
                sentiment_score = 0.0
                confidence = 0.8
                matched = False
                
                for asset in result.get("assets", []):
                    asset_ticker = asset.get("ticker", "")
                    if asset_ticker and asset_ticker.strip().upper() == ticker.upper():
                        sentiment_score = float(asset.get("sentiment_score", 0.0))
                        confidence = float(asset.get("confidence", 0.8))
                        matched = True
                        break
                        
                if not matched and result.get("assets"):
                    first_asset = result["assets"][0]
                    sentiment_score = float(first_asset.get("sentiment_score", 0.0))
                    confidence = float(first_asset.get("confidence", 0.8))
                elif not matched:
                    sentiment_score = float(result.get("overall_sentiment", 0.0))
                    
                final_engagement = max(total_engagement, len(subposts) * 10)
                
                cur.execute(upsert_sql, (
                    "twitter",
                    channel,
                    ticker,
                    asset_name,
                    bucket_ts,
                    sentiment_score,
                    confidence,
                    len(subposts),
                    final_engagement,
                    summary_text[:1000],
                    content_hash
                ))
                conn.commit()
                scored_count += 1
                print(f" [ok] Saved Tweet Sentiment: {ticker} | Sentiment: {sentiment_score:.2f} | Conf: {confidence:.2f} | Engagement: {final_engagement}")
            except Exception as e:
                print(f" [error] Error scoring/saving tweet sentiment: {e}")
                
    cur.close()
    conn.close()
    print(f"\n[OK] Scraped and scored {scored_count} FinTwit asset buckets.")

if __name__ == "__main__":
    main()
