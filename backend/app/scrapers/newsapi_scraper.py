# backend/app/scrapers/newsapi_scraper.py
import hashlib
import requests
from datetime import datetime, timezone
from backend.app.config import get_settings

def get_hash(text: str) -> str:
    return hashlib.md5(text.strip().encode('utf-8', errors='replace')).hexdigest()

def fetch_newsapi_articles() -> list:
    settings = get_settings()
    api_key = settings.newsapi_key
    if not api_key:
        print("[NewsAPI] No API key found. Skipping NewsAPI fetch.")
        return []

    print("[NewsAPI] Fetching business headlines...")
    url = f"https://newsapi.org/v2/top-headlines?category=business&language=en&apiKey={api_key}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        
        records = []
        for art in articles:
            title = art.get("title") or ""
            link = art.get("url") or ""
            if not title or not link:
                continue
            
            pub_ts = art.get("publishedAt")
            if pub_ts:
                try:
                    # NewsAPI usually returns ISO-8601 strings
                    pub_ts = datetime.fromisoformat(pub_ts.replace("Z", "+00:00")).isoformat()
                except Exception:
                    pass

            records.append({
                "source_name": art.get("source", {}).get("name") or "NewsAPI",
                "feed_url": "https://newsapi.org/",
                "title": title.strip(),
                "link": link.strip(),
                "published_raw": art.get("publishedAt"),
                "published_ts": pub_ts,
                "summary": art.get("description") or art.get("content") or "",
                "url_hash": get_hash(link),
                "title_hash": get_hash(title.lower())
            })
        print(f"[NewsAPI] Successfully fetched {len(records)} articles.")
        return records
    except Exception as e:
        print(f"[NewsAPI] Error fetching articles: {e}")
        return []

def fetch_gnews_articles() -> list:
    settings = get_settings()
    api_key = settings.gnews_api_key
    if not api_key:
        print("[GNews] No API key found. Skipping GNews fetch.")
        return []

    print("[GNews] Fetching business headlines...")
    url = f"https://gnews.io/api/v4/top-headlines?category=business&lang=en&apikey={api_key}"
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        data = response.json()
        articles = data.get("articles", [])
        
        records = []
        for art in articles:
            title = art.get("title") or ""
            link = art.get("url") or ""
            if not title or not link:
                continue
            
            pub_ts = art.get("publishedAt")
            if pub_ts:
                try:
                    # GNews returns ISO-8601
                    pub_ts = datetime.fromisoformat(pub_ts.replace("Z", "+00:00")).isoformat()
                except Exception:
                    pass

            records.append({
                "source_name": art.get("source", {}).get("name") or "GNews",
                "feed_url": "https://gnews.io/",
                "title": title.strip(),
                "link": link.strip(),
                "published_raw": art.get("publishedAt"),
                "published_ts": pub_ts,
                "summary": art.get("description") or art.get("content") or "",
                "url_hash": get_hash(link),
                "title_hash": get_hash(title.lower())
            })
        print(f"[GNews] Successfully fetched {len(records)} articles.")
        return records
    except Exception as e:
        print(f"[GNews] Error fetching articles: {e}")
        return []
