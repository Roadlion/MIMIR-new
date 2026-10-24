# backend/app/scrapers/rss_scraper.py
import sys
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

import warnings
import feedparser
import requests
import time
import random
import hashlib
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime
from urllib.parse import urlparse
import psycopg2
from psycopg2.extras import execute_values
from backend.app.database import get_db_connection

# ponytail: conda OpenSSL can't verify certs on this machine; skip SSL for dev scraping
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_SCRAPER_SESSION = requests.Session()
_SCRAPER_SESSION.verify = False

FINANCIAL_RSS_FEEDS = [
    # --- Regional Financial News (Thailand/Asia) ---
    "https://www.bangkokpost.com/rss/data/business.xml",
    "https://thestandard.co/category/wealth/feed/",
    "https://www.kaohoon.com/feed",
    "https://www.moneybuffalo.in.th/feed",
    "https://www.thansettakij.com/rss/feed/money_market",
    "https://www.scb.co.th/en/about-us/news/rss.xml",
    "https://www.dealstreetasia.com/feed",
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
    "https://www.caixinglobal.com/rss/", # Caixin Global (China)
    "https://www.moneycontrol.com/rss/MC_latestnews.xml", # India Finance
    "https://mainichi.jp/english/rss/etc/business.xml", # Japan Business

    # --- Regional Financial News (Other Regions) ---
    "https://www.smh.com.au/rss/business.xml", # Sydney Morning Herald (Australia)
    "https://lataminvestor.com/feed/", # LatAm Investor (Latin America)
    "https://african.business/feed/", # African Business (Africa)
    "https://financialpost.com/feed/", # Financial Post (Canada)
    "https://www.telegraph.co.uk/business/rss.xml", # UK Business
    "https://www.euractiv.com/sections/economy-jobs/feed/", # EU Economy & Jobs
    "https://www.economist.com/the-americas/rss.xml", # LatAm Economist
    "https://www.zawya.com/en/rss/all-news", # Middle East Business
    "https://www.arabianbusiness.com/feed", # Arabian Business Middle East

    # --- General Financial / Markets News ---
    "https://finance.yahoo.com/news/rss",
    "http://feeds.reuters.com/reuters/businessNews",
    "http://feeds.reuters.com/reuters/financialsNews",
    "http://feeds.marketwatch.com/marketwatch/topstories",
    "http://feeds.marketwatch.com/marketwatch/marketpulse",
    "https://www.ft.com/?format=rss",
    "https://www.ft.com/companies?format=rss",
    "https://www.ft.com/world?format=rss",
    "https://www.ft.com/global-economy?format=rss",
    "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069",
    "https://www.cnbc.com/id/10000067/device/rss/rss.xml", # CNBC Finance
    "https://www.cnbc.com/id/100003114/device/rss/rss.xml", # CNBC Top News
    "https://www.cnbc.com/id/10001147/device/rss/rss.xml", # CNBC Markets
    "https://seekingalpha.com/feed.xml",
    "https://www.investing.com/rss/news_285.rss",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml", # WSJ US Business
    "https://feeds.a.dj.com/rss/RSSOpinion.xml", # WSJ Opinion
    "https://www.economist.com/finance-and-economics/rss.xml",
    "https://techcrunch.com/feed/",
    "https://www.bloomberg.com/feeds/bproperty.xml",
    "https://www.theguardian.com/business/rss",
    "https://www.cnbc.com/id/19794221/device/rss/rss.xml",
    "https://www.cnbc.com/id/10000115/device/rss/rss.xml",
    "https://www.forbes.com/business/feed/",
    "https://www.forbes.com/money/feed/",
    "https://www.nasdaq.com/feed/rssoutbound?category=Markets",

    # --- Crypto / Digital Assets News ---
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://bitcoinmagazine.com/.rss/full/", # Bitcoin Magazine
    "https://cryptonews.com/news/feed/", # Cryptonews
    "https://www.cryptoglobe.com/latest/feed/", # CryptoGlobe
    "https://coinjournal.net/news/feed/", # CoinJournal
    "https://blockworks.co/feed", # Blockworks
    "https://decrypt.co/feed", # Decrypt
    "https://www.theblock.co/rss.xml", # The Block
    "https://cryptoslate.com/feed", # CryptoSlate

    # --- Macroeconomic Policy / Geopolitics / Breaking News ---
    "https://www.federalreserve.gov/feeds/press_all.xml", # Federal Reserve (Rates & Decisions)
    "https://www.ecb.europa.eu/press/pr/shared/pdf/ecb.pr.rss.xml", # European Central Bank Press Releases
    "https://www.bankofengland.co.uk/rss/news", # Bank of England News
    "https://www.imf.org/en/News/RSSFeeds/PressReleases", # IMF Press Releases
    "https://services.reuters.com/info/us/num/world.xml", # Reuters World News
    "http://feeds.bbci.co.uk/news/world/rss.xml", # BBC World News (Geopolitical conflicts)
    "https://newsfeed.ap.org/rss/v2/featured", # AP Top News
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml", # WSJ World News
    "https://www.aljazeera.com/xml/rss/all.xml", # Al Jazeera (Geopolitical events)

    # --- Added Feeds ---
    "https://cdn.businesskorea.co.kr/rss/gns_allArticle.xml",
    "https://www.interad.com/en/insights/feed",
    "https://bcck.or.kr/bcck/feed/?ckattempt=1",
    "https://www.scmp.com/rss/91/feed",
    "https://www.scmp.com/rss/10/feed",
    "https://www.scmp.com/rss/96/feed",
    "https://www.scmp.com/rss/7/feed",
    "https://www.scmp.com/rss/12/feed",
    "https://www.scmp.com/rss/318421/feed",
    "https://www.scmp.com/rss/318200/feed",
    "https://www.scmp.com/rss/36/feed",
    "https://www.scmp.com/rss/320663/feed",
    "https://www.scmp.com/rss/318218/feed",
    "https://www.scmp.com/rss/318219/feed",
    "https://www.scmp.com/rss/318220/feed",
    "https://www.scmp.com/rss/318222/feed",
    "https://www.scmp.com/rss/318223/feed",
    "https://www.scmp.com/rss/323047/feed",
    "https://www.scmp.com/rss/323048/feed",

    # --- Geographically Diverse Global Business Feeds ---
    "https://en.mercopress.com/rss/economy",
    "https://www.latinfinance.com/rss",
    "https://latamlist.com/feed/",
    "https://www.financeasia.com/rss/latest",
    "https://vneconomy.vn/tai-chinh.rss",
    "https://www.stuff.co.nz/business/feed",
    "https://www.macaubusiness.com/feed/",
    "https://seenews.com/news/feed",
    "https://eng.lsm.lv/rss/?lang=en&catid=21659",
    "https://www.nasdaqomxnordic.com/rss/nordicnews",
    "https://www.eu-startups.com/feed/",
    "https://mid-east.info/feed/",
    "https://www.arabianbusiness.com/industries/technology/feed",
    "https://www.howwemadeitinafrica.com/feed",
    "https://www.africanews.com/feed/category/business/rss",
    "https://allafrica.com/tools/headlines/rdf/business/headlines.rdf",
    "https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/business/",
    "https://www.newyorkfed.org/xml/rss/research/index.xml"
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


from concurrent.futures import ThreadPoolExecutor, as_completed

def _fetch_single_feed(url: str, headers: dict) -> Tuple[str, str, List[dict], str]:
    domain = urlparse(url).netloc
    try:
        resp = _SCRAPER_SESSION.get(url, headers=headers, timeout=(3.0, 5.0))
        if resp.status_code == 429:
            return url, domain, [], "rate_limited"
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        return url, domain, getattr(feed, 'entries', []), "ok"
    except Exception as e:
        return url, domain, [], str(e)


def fetch_financial_feed_batch(feeds_list=FINANCIAL_RSS_FEEDS, max_workers=10):
    """
    Scrape ALL articles from RSS feeds concurrently with strict timeouts and robust deduplication.
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
    print("📰 MIMIR RSS SCRAPER (Parallel Fast Scrape)")
    print(f"   Existing title hashes in DB: {len(existing_title_hashes)}")
    print(f"   Total feeds to scrape: {len(feeds_list)}")
    print("="*60)

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(_fetch_single_feed, url, headers): url for url in feeds_list}
        for future in as_completed(future_to_url):
            try:
                url, domain, entries, status = future.result(timeout=10)
                results.append((url, domain, entries, status))
            except Exception as e:
                url = future_to_url[future]
                domain = urlparse(url).netloc
                results.append((url, domain, [], f"timeout/error: {e}"))

    for url, domain, entries, status in results:
        total_feeds += 1
        if status != "ok":
            print(f"⚠️ {domain} -> {status}")
            continue

        feed_total = 0
        feed_duplicates = 0

        for entry in entries:
            feed_total += 1
            total_articles += 1

            title = entry.get('title', '').strip()
            summary = entry.get('summary', '').strip()
            link = entry.get('link')

            if not link or not title:
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

            # --- 3. CHECK DUPLICATE TITLE IN BATCH ---
            title_key = title[:60].lower().strip()
            if title_key in seen_titles:
                feed_duplicates += 1
                batch_duplicates += 1
                continue
            seen_titles.add(title_key)

            # --- 4. CHECK DUPLICATE TITLE IN DATABASE ---
            if title_hash in existing_title_hashes:
                feed_duplicates += 1
                batch_duplicates += 1
                db_duplicates += 1
                continue
            existing_title_hashes.add(title_hash)

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
                "title_hash": title_hash
            }
            normalized_records.append(record)

        if feed_total > 0:
            print(f"📡 {domain} → {feed_total} articles (skipped {feed_duplicates})")

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