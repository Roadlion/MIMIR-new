import requests
import xml.etree.ElementTree as ET
from typing import List, Dict
import logging
import random
from ..sentiment.deepseek_client import DeepSeekSentiment
import xml.etree.ElementTree as ET
from typing import List, Dict
import logging
from ..sentiment.deepseek_client import DeepSeekSentiment

logger = logging.getLogger(__name__)

# Fallback/Mock data if actual RSS is unreachable during demo
MOCK_FEEDS = {
    "usda": [
        {"title": "USDA WASDE: Wheat Yields Drop Unexpectedly", "summary": "Severe drought in the Midwest has slashed projected wheat yields by 12% for the upcoming harvest, driving concerns of supply shortages."},
        {"title": "Corn Stocks Remain High", "summary": "Record corn planting has resulted in oversupplied silos across the corn belt, depressing local cash prices."}
    ],
    "noaa": [
        {"title": "La Nina Pattern Intensifies", "summary": "Unseasonably dry conditions are expected to persist across the American plains, threatening winter crops."},
        {"title": "Gulf Coast Hurricane Warning", "summary": "Category 3 storm threatens to disrupt shipping lanes and offshore rigs in the Gulf of Mexico."}
    ],
    "shipping": [
        {"title": "Panama Canal Drought Restrictions", "summary": "Low water levels force authorities to cut daily vessel transits by 20%, spiking dry bulk freight rates."},
        {"title": "Vessel Backlog Clears at Rotterdam", "summary": "Port operations have normalized after last week's strike, easing supply chain bottlenecks in Europe."}
    ]
}

def fetch_rss(url: str, source_type: str) -> List[Dict]:
    """Fetches an RSS feed and returns parsed items. Uses mock data if it fails."""
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = []
        for item in root.findall('.//item')[:1]:  # Just 1 item per feed to avoid LLM rate limits
            title = item.find('title').text if item.find('title') is not None else ""
            desc = item.find('description').text if item.find('description') is not None else ""
            items.append({"title": title, "summary": desc})
        return items if items else MOCK_FEEDS.get(source_type, [])
    except Exception as e:
        logger.warning(f"Failed to fetch {source_type} RSS from {url}: {e}. Using fallback data.")
        return []

def fetch_weather_api() -> List[Dict]:
    """Fetches active weather alerts from weather.gov API."""
    try:
        headers = {"User-Agent": "MIMIR-GuerillaQuant/1.0"}
        res = requests.get("https://api.weather.gov/alerts/active?limit=3", headers=headers, timeout=5)
        res.raise_for_status()
        data = res.json()
        items = []
        for feature in data.get("features", []):
            props = feature.get("properties", {})
            title = props.get("headline", "Weather Alert")
            desc = props.get("description", "")
            items.append({"title": title, "summary": desc})
        return items if items else MOCK_FEEDS.get("noaa", [])
    except Exception as e:
        logger.warning(f"Failed to fetch weather.gov API: {e}. Using fallback.")
        return MOCK_FEEDS.get("noaa", [])

def scrape_niche_sentiment() -> Dict[str, float]:
    """
    Scrapes niche sources (USDA, NOAA, Shipping) and scores them using DeepSeek.
    Returns a mapping of Ticker -> Average Sentiment Score.
    """
    all_sources = [
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
        {"url": "https://www.hellenicshippingnews.com/feed/", "type": "shipping"}
    ]
    
    # Randomly sample 3 RSS feeds to avoid rate-limiting the LLM during a synchronous scan
    sources = random.sample(all_sources, min(3, len(all_sources)))
    
    analyzer = DeepSeekSentiment()
    sentiment_map = {}
    
    # Process sampled RSS feeds
    for src in sources:
        articles = fetch_rss(src["url"], src["type"])
        for article in articles:
            # Re-use the existing DeepSeek sentiment pipeline
            result = analyzer.score_article_with_assets(article["title"], article["summary"])
            
            # Map sentiment back to tickers
            if "assets" in result:
                for asset in result["assets"]:
                    ticker = asset.get("ticker")
                    if ticker:
                        score = asset.get("sentiment_score", 0.0)
                        if ticker not in sentiment_map:
                            sentiment_map[ticker] = []
                        sentiment_map[ticker].append(score)
                        
    # Process Weather API
    weather_articles = fetch_weather_api()
    for article in weather_articles:
        result = analyzer.score_article_with_assets(article["title"], article["summary"])
        if "assets" in result:
            for asset in result["assets"]:
                ticker = asset.get("ticker")
                if ticker:
                    score = asset.get("sentiment_score", 0.0)
                    if ticker not in sentiment_map:
                        sentiment_map[ticker] = []
                    sentiment_map[ticker].append(score)
    
    # Average the scores
    avg_sentiment = {}
    for ticker, scores in sentiment_map.items():
        avg_sentiment[ticker] = sum(scores) / len(scores)
        
    return avg_sentiment
