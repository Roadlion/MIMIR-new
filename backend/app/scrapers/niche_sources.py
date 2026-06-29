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
    # — Agriculture — additional free sources
    {"url": "https://www.agriculture.com/rss/news", "type": "usda"},
    {"url": "https://www.farmprogress.com/rss", "type": "usda"},
    # — Energy —
    {"url": "https://oilprice.com/rss/main", "type": "energy"},
    {"url": "https://www.lngworldnews.com/feed/", "type": "energy"},
    {"url": "https://www.world-nuclear-news.org/rss/news", "type": "energy"},
    # — Shipping —
    {"url": "https://www.hellenicshippingnews.com/feed/", "type": "shipping"},
    {"url": "https://splash247.com/feed/", "type": "shipping"},
    {"url": "https://theloadstar.com/feed/", "type": "shipping"},
    {"url": "https://www.drybulkmagazine.com/feed/", "type": "shipping"},
    # — Metals / Mining —
    {"url": "https://www.miningweekly.com/page/feed", "type": "metals"},
    {"url": "https://www.mining.com/feed/", "type": "metals"},
    # — Weather (API, not RSS) —
    # weather.gov handled separately via fetch_weather_api()
]


def fetch_rss(url: str, source_type: str) -> List[Dict]:
    """Fetch an RSS feed and return parsed articles."""
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = []
        for item in root.findall('.//item')[:3]:  # up to 3 per feed
            title = item.findtext('title', default="")
            desc = item.findtext('description', default="")
            pub = item.findtext('pubDate', default="")
            items.append({"title": title, "summary": desc, "published_raw": pub, "source_type": source_type})
        return items if items else MOCK_FEEDS.get(source_type, [])
    except Exception as e:
        logger.warning(f"Failed to fetch {source_type} RSS from {url}: {e}")
        return MOCK_FEEDS.get(source_type, [])


def fetch_weather_api() -> List[Dict]:
    """Fetch active US weather alerts from weather.gov API."""
    try:
        headers = {"User-Agent": "MIMIR-GuerillaQuant/1.0"}
        res = requests.get("https://api.weather.gov/alerts/active?limit=3", headers=headers, timeout=5)
        res.raise_for_status()
        data = res.json()
        items = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            items.append({
                "title": props.get("headline", "Weather Alert"),
                "summary": props.get("description", ""),
                "published_raw": props.get("sent", ""),
                "source_type": "noaa",
            })
        return items if items else MOCK_FEEDS.get("noaa", [])
    except Exception as e:
        logger.warning(f"Failed to fetch weather.gov API: {e}")
        return MOCK_FEEDS.get("noaa", [])


def scrape_niche_articles(sample_size: int = 5) -> List[Dict]:
    """
    Scrape articles from niche market RSS feeds and weather API.
    Returns a flat list of dicts each containing:
        title, summary, published_raw, source_type, source_url
    Does NOT call DeepSeek — the caller is responsible for sentiment scoring.
    """
    sources = random.sample(NICHE_RSS_FEEDS, min(sample_size, len(NICHE_RSS_FEEDS)))
    articles = []

    for src in sources:
        items = fetch_rss(src["url"], src["type"])
        for item in items:
            item["source_url"] = src["url"]
            articles.append(item)

    # Add weather API
    weather_items = fetch_weather_api()
    for item in weather_items:
        articles.append(item)

    return articles
