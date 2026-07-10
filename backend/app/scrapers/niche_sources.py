"""Scrape niche market news from free RSS / API sources for Guerilla Quant."""
import requests
import xml.etree.ElementTree as ET
from typing import List, Dict
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MOCK_FEEDS = {
    "usda": [
        {"title": "USDA WASDE: Wheat Yields Drop Unexpectedly", "summary": "Severe drought in the Midwest has slashed projected wheat yields by 12%."},
        {"title": "Corn Stocks Remain High", "summary": "Record corn planting has resulted in oversupplied silos across the corn belt."},
    ],
    "noaa": [
        {"title": "La Nina Pattern Intensifies", "summary": "Unseasonably dry conditions expected to persist across the American plains."},
        {"title": "Gulf Coast Hurricane Warning", "summary": "Category 3 storm threatens to disrupt shipping lanes and offshore rigs."},
    ],
    "shipping": [
        {"title": "Panama Canal Drought Restrictions", "summary": "Low water levels force authorities to cut daily vessel transits by 20%."},
        {"title": "Vessel Backlog Clears at Rotterdam", "summary": "Port operations normalized after last week's strike."},
    ],
    "energy": [
        {"title": "OPEC+ Extends Production Cuts", "summary": "OPEC+ agrees to extend voluntary production cuts through Q3, supporting crude prices."},
        {"title": "Nuclear Output Rises in Asia", "summary": "New reactor approvals drive growth in nuclear energy capacity across Southeast Asia."},
    ],
    "metals": [
        {"title": "Copper Supply Tightens", "summary": "Global copper inventories hit multi-year lows as demand from renewable sectors surges."},
        {"title": "Gold Demand Surges on Rate Cut Bets", "summary": "Central bank buying and ETF inflows push gold demand to record levels."},
    ],
}

NICHE_RSS_FEEDS = [
    # — Agriculture / USDA —
    {"url": "https://www.ars.usda.gov/rss/?productName=Research%20News", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Crops&x=61&y=35", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Climate+Change&x=95&y=9", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Food+Safety&x=12&y=36", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Animals&x=59&y=13", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Nutrition+and+Health&x=44&y=11", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Biofuels&x=56&y=21", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Bio+Products&x=41&y=17", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Organics&x=8&y=11", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Bees&x=51&y=15", "type": "usda"},
    {"url": "https://www.ars.usda.gov/rss/?topic=Invasive+Species&x=62&y=17", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/newsroom", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/federal-register", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/policy-memo-snap", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/policy-memo-wic", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/policy-memo-fmnp", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/policy-memo-fdp", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/policy-memo-cnp", "type": "usda"},
    {"url": "https://www.fna.usda.gov/rss-feeds/policy-memo-sfmnp", "type": "usda"},
    {"url": "https://www.usda.gov/rss/home.xml", "type": "usda"},
    # — Energy —
    {"url": "https://oilprice.com/rss/main", "type": "energy"},
    {"url": "https://www.eia.gov/todayinenergy/feed.xml", "type": "energy"},
    # — Shipping —
    {"url": "https://www.hellenicshippingnews.com/feed/", "type": "shipping"},
    {"url": "https://splash247.com/feed/", "type": "shipping"},
    {"url": "https://theloadstar.com/feed/", "type": "shipping"},
    # — Metals / Mining —
    {"url": "https://www.miningweekly.com/page/home/feed", "type": "metals"},
    {"url": "https://www.mining.com/feed/", "type": "metals"},
]


def fetch_rss(url: str, source_type: str) -> List[Dict]:
    """Fetch an RSS/Atom feed and return parsed articles robustly."""
    import re
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=12, verify=False)
        response.raise_for_status()
        content_str = response.text
        
        # Try XML standard parser first
        try:
            # Preprocess XML to replace common problematic entities
            clean_xml = content_str.replace("&nbsp;", " ").replace("&", "&amp;")
            clean_xml = clean_xml.replace("&amp;amp;", "&amp;") # Fix double encoding if it happened
            
            root = ET.fromstring(clean_xml.encode('utf-8'))
            items = []
            
            # Support both RSS <item> and Atom <entry>
            xml_items = root.findall('.//item')
            if not xml_items:
                xml_items = root.findall('.//entry')
                
            for item in xml_items[:3]:  # up to 3 per feed
                title = ""
                desc = ""
                pub = ""
                for child in item:
                    tag_local = child.tag.split('}')[-1]
                    if tag_local == 'title':
                        title = child.text or ""
                    elif tag_local in ['description', 'summary', 'content']:
                        desc = child.text or ""
                    elif tag_local in ['pubDate', 'published', 'updated']:
                        pub = child.text or ""
                
                # Clean CDATA wraps: <![CDATA[ ... ]]>
                title = re.sub(r'^<!\[CDATA\[([\s\S]*?)\]\]>$', r'\1', title).strip()
                desc = re.sub(r'^<!\[CDATA\[([\s\S]*?)\]\]>$', r'\1', desc).strip()
                
                # Clean HTML tags
                title = re.sub(r'<[^>]*>', '', title).strip()
                desc = re.sub(r'<[^>]*>', '', desc).strip()
                
                if title or desc:
                    items.append({"title": title, "summary": desc, "published_raw": pub, "source_type": source_type})
            if items:
                return items
        except Exception as parse_err:
            logger.warning(f"Standard XML parsing failed for {url}: {parse_err}. Attempting regex fallback...")
            
        # Regex fallback parser
        items = []
        blocks = re.findall(r'<(item|entry)>([\s\S]*?)<\/\1>', content_str)
        for _, block in blocks[:3]:
            title_match = re.search(r'<title[^>]*>([\s\S]*?)<\/title>', block)
            desc_match = re.search(r'<(description|summary|content)[^>]*>([\s\S]*?)<\/\1>', block)
            pub_match = re.search(r'<(pubDate|published|updated)[^>]*>([\s\S]*?)<\/\1>', block)
            
            title = title_match.group(1).strip() if title_match else ""
            desc = desc_match.group(2).strip() if desc_match else ""
            pub = pub_match.group(1).strip() if pub_match else ""
            
            title = re.sub(r'^<!\[CDATA\[([\s\S]*?)\]\]>$', r'\1', title).strip()
            desc = re.sub(r'^<!\[CDATA\[([\s\S]*?)\]\]>$', r'\1', desc).strip()
            
            title = re.sub(r'<[^>]*>', '', title).strip()
            desc = re.sub(r'<[^>]*>', '', desc).strip()
            
            if title or desc:
                items.append({"title": title, "summary": desc, "published_raw": pub, "source_type": source_type})
        return items
    except Exception as e:
        logger.warning(f"Failed to fetch {source_type} RSS from {url}: {e}")
        return []


def fetch_weather_api() -> List[Dict]:
    """Fetch active US weather alerts from weather.gov API."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        # Standard browser headers (weather.gov API requires a User-Agent or it throws 403/400)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        res = requests.get("https://api.weather.gov/alerts/active", headers=headers, timeout=8, verify=False)
        res.raise_for_status()
        data = res.json()
        items = []
        for feature in data.get("features", [])[:3]:  # Slice first 3 here
            props = feature.get("properties", {})
            items.append({
                "title": props.get("headline", "Weather Alert"),
                "summary": props.get("description", ""),
                "published_raw": props.get("sent", ""),
                "source_type": "noaa",
            })
        return items
    except Exception as e:
        logger.warning(f"Failed to fetch weather.gov API: {e}")
        return []


def scrape_niche_articles(sample_size: int = 30) -> List[Dict]:
    """
    Scrape articles from niche market RSS feeds and weather API.
    Uses ThreadPoolExecutor to scrape feeds concurrently.
    """
    from concurrent.futures import ThreadPoolExecutor
    
    # Sample up to sample_size feeds
    sources = random.sample(NICHE_RSS_FEEDS, min(sample_size, len(NICHE_RSS_FEEDS)))
    articles = []

    def fetch_single(src):
        try:
            items = fetch_rss(src["url"], src["type"])
            for item in items:
                item["source_url"] = src["url"]
            return items
        except Exception:
            return []

    # Fetch concurrently
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = executor.map(fetch_single, sources)
        for res in results:
            if res:
                articles.extend(res)

    # Add weather API
    weather_items = fetch_weather_api()
    if weather_items:
        for item in weather_items:
            item["source_url"] = "https://api.weather.gov/alerts/active"
            articles.append(item)

    # Fallback to MOCK_FEEDS if absolutely nothing was fetched (e.g. offline)
    if not articles:
        logger.info("No live niche articles scraped. Loading mock feeds fallback.")
        for src in sources:
            mock_list = MOCK_FEEDS.get(src["type"], [])
            for m in mock_list:
                m_copy = m.copy()
                m_copy["source_url"] = src["url"]
                m_copy["published_raw"] = datetime.now(timezone.utc).isoformat()
                articles.append(m_copy)
                
        # Mock weather
        for m in MOCK_FEEDS.get("noaa", []):
            m_copy = m.copy()
            m_copy["source_url"] = "https://api.weather.gov/alerts/active"
            m_copy["published_raw"] = datetime.now(timezone.utc).isoformat()
            articles.append(m_copy)

    return articles

