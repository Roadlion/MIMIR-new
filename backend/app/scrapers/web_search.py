import os
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from .newsapi_scraper import fetch_newsapi_articles
from ..config import get_settings

def fetch_page_content(url, max_chars=3000):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove script and style elements
        for script in soup(["script", "style", "nav", "footer", "header"]):
            script.extract()
            
        text = soup.get_text(separator=' ', strip=True)
        return text[:max_chars]
    except Exception as e:
        return ""

def search_ddg(query, num_results=3):
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=num_results):
                content = fetch_page_content(r['href'])
                if not content:
                    content = r.get('body', '')
                results.append({
                    'title': r.get('title', ''),
                    'url': r.get('href', ''),
                    'content': content
                })
    except Exception as e:
        print(f"DDG Search failed: {e}")
    return results

def search_tavily(query, num_results=3):
    settings = get_settings()
    api_key = settings.tavily_api_key
    if not api_key:
        return []
    
    try:
        response = requests.post(
            'https://api.tavily.com/search',
            json={'query': query, 'api_key': api_key, 'max_results': num_results, 'include_raw_content': False},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        return [{'title': res['title'], 'url': res['url'], 'content': res['content']} for res in data.get('results', [])]
    except Exception as e:
        print(f"Tavily Search failed: {e}")
        return []

def perform_tiered_search(query, num_results=3):
    """
    Tier 1: DuckDuckGo Search (Unlimited Free)
    Tier 2: Tavily (If DDG fails, uses free tier API key)
    Tier 3: NewsAPI fallback
    """
    print(f"[Search] Executing Tier 1 (DDG) for: {query}")
    results = search_ddg(query, num_results)
    
    if not results:
        print(f"[Search] Tier 1 failed/empty. Executing Tier 2 (Tavily)...")
        results = search_tavily(query, num_results)
        
    if not results:
        print(f"[Search] Tier 2 failed/empty. Executing Tier 3 (NewsAPI)...")
        # Just searching NewsAPI directly with the query
        try:
            # We would need to adjust fetch_newsapi_articles to accept a query, 
            # for now, we just return empty or a generic error.
            pass
        except Exception:
            pass

    return results
