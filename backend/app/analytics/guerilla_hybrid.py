from .cointegration import calculate_z_score
from ..scrapers.niche_sources import scrape_niche_sentiment

# Cache to avoid calling DeepSeek on every request during testing
_sentiment_cache = None

def get_hybrid_signals():
    """
    Scans predefined niche pairs for stat-arb opportunities,
    and overlays LLM-derived sentiment from niche sources to determine Conviction.
    """
    global _sentiment_cache
    
    pairs = [
        ("CORN", "WEAT"),
        ("BDRY", "SBLK"),
        ("URA", "NLR")
    ]
    
    # Fetch sentiment once and cache it for the session
    if _sentiment_cache is None:
        try:
            _sentiment_cache = scrape_niche_sentiment()
        except Exception as e:
            print(f"Failed to scrape niche sentiment: {e}")
            _sentiment_cache = {}

    opportunities = []
    for t1, t2 in pairs:
        res = calculate_z_score(t1, t2)
        if not res:
            continue
            
        # Get sentiment for the individual tickers if available
        s1 = _sentiment_cache.get(t1, 0.0)
        s2 = _sentiment_cache.get(t2, 0.0)
        
        # Calculate a combined sentiment filter score
        # If we are Short T1 / Long T2, we want Sentiment T1 to be negative and Sentiment T2 to be positive.
        # This implies (s2 - s1) should be positive for a Short T1/Long T2 trade.
        sentiment_delta = s2 - s1 
        
        z_score = res["z_score"]
        signal = res["signal"]
        
        conviction = "LOW"
        
        if signal.startswith("SHORT"):
            # Z-Score > 2.0. We want T1 to drop and T2 to rise.
            # So sentiment_delta (S2 - S1) should be > 0
            if sentiment_delta > 0.2:
                conviction = "HIGH (Math + Sentiment Aligned)"
            elif sentiment_delta < -0.2:
                conviction = "WARNING (Sentiment Contradicts Math)"
            else:
                conviction = "MEDIUM (Math Only)"
                
        elif signal.startswith("LONG"):
            # Z-Score < -2.0. We want T1 to rise and T2 to drop.
            # So sentiment_delta (S2 - S1) should be < 0
            if sentiment_delta < -0.2:
                conviction = "HIGH (Math + Sentiment Aligned)"
            elif sentiment_delta > 0.2:
                conviction = "WARNING (Sentiment Contradicts Math)"
            else:
                conviction = "MEDIUM (Math Only)"
                
        res["sentiment_t1"] = round(s1, 2)
        res["sentiment_t2"] = round(s2, 2)
        res["conviction"] = conviction
        
        opportunities.append(res)
        
    return opportunities
