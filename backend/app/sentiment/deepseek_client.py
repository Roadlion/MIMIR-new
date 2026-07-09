# backend/app/sentiment/deepseek_client.py
import requests
import json
import re
import time
import logging
import random
from typing import Dict, List, Optional, Tuple
from ..config import get_settings
from .asset_mapper import resolve_ticker, resolve_country_code, resolve_region, ASSET_TO_TICKER
from .llm_client import send_chat_completion

settings = get_settings()
logger = logging.getLogger(__name__)


class DeepSeekSentiment:
    def __init__(self):
        self.api_key = settings.deepseek_api_key
        self.base_url = settings.deepseek_base_url
        self.model = settings.deepseek_model
        self.min_confidence = getattr(settings, "deepseek_min_confidence", 0.40)
        self._cache = {}
        # Build canonical name map from asset_mapper keys (lowercase -> original)
        self._canonical_name = {
            name.lower(): name for name in ASSET_TO_TICKER.keys()
        }
        # Build ticker-to-sector map from HEATMAP_INDICES constituents
        self._ticker_to_sector = {}
        try:
            from ..routers.prices import HEATMAP_INDICES
            gics_to_canonical = {
                "technology": "TECHNOLOGY",
                "comm. services": "COMMUNICATION_SERVICES",
                "communication services": "COMMUNICATION_SERVICES",
                "consumer cycl.": "CONSUMER_CYCLICAL",
                "consumer cyclical": "CONSUMER_CYCLICAL",
                "financials": "FINANCIAL_SERVICES",
                "financial services": "FINANCIAL_SERVICES",
                "healthcare": "HEALTHCARE",
                "consumer def.": "CONSUMER_DEFENSIVE",
                "consumer defensive": "CONSUMER_DEFENSIVE",
                "energy": "ENERGY",
                "industrials": "INDUSTRIALS",
                "materials": "BASIC_MATERIALS",
                "basic materials": "BASIC_MATERIALS",
                "real estate": "REAL_ESTATE",
                "utilities": "UTILITIES"
            }
            for idx_info in HEATMAP_INDICES.values():
                for c in idx_info.get("constituents", []):
                    t = c.get("ticker")
                    s = c.get("sector")
                    if t and s:
                        canon = gics_to_canonical.get(s.lower())
                        if canon:
                            self._ticker_to_sector[t.lower().strip()] = canon
        except Exception as ex:
            logger.warning(f"Failed to build ticker-to-sector map: {ex}")

    def _normalize_asset_name(self, name: str) -> str:
        """Return canonical asset name if known, else the original."""
        if not name:
            return name
        lower = name.lower().strip()
        return self._canonical_name.get(lower, name)

    def _enrich_asset(self, asset: Dict) -> Dict:
        """Add ticker, normalize name, and optionally fill missing country/region."""
        # Normalize asset name
        canonical = self._normalize_asset_name(asset.get("asset_name", ""))
        asset["asset_name"] = canonical

        # Add ticker
        ticker, found = resolve_ticker(canonical)
        asset["ticker"] = ticker if found else None

        # For equities, map sector from HEATMAP_INDICES constituents if available
        if asset.get("asset_category") == "EQUITY" and asset.get("ticker"):
            ticker_lower = asset["ticker"].lower().strip()
            if ticker_lower in self._ticker_to_sector:
                asset["sub_category"] = self._ticker_to_sector[ticker_lower]

        # For commodities, ensure country/region are null (already handled in validation)
        return asset

    # ============================================================
    # MAIN ENTRY: Per-asset sentiment with strict rules
    # ============================================================
    def score_article_with_assets(self, title: str, summary: str, force_relevance: bool = False) -> Dict:
        """
        Send article to LLM for multi-asset sentiment scoring.
        """
        key = (title, summary)
        if key in self._cache:
            logger.debug("Returning cached result for article")
            return self._cache[key]

        # Check relevance filter first (unless force_relevance is True) to avoid unnecessary LLM calls
        if not force_relevance and not self.is_financial_or_macro(title, summary):
            logger.info(f"Filtering irrelevant/non-financial article: {title[:60]}...")
            empty_result = {"overall_sentiment": 0.0, "assets": []}
            self._cache[key] = empty_result
            return empty_result

        system_prompt = self._get_system_prompt()
        user_prompt = self._build_user_prompt(title, summary)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        try:
            content = send_chat_completion(
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=60
            )

            result = self._parse_json_response(content)
            assets = result.get("assets", [])
            if not isinstance(assets, list):
                assets = []

            # Validate and enrich assets
            validated_assets = self._validate_assets(assets)

            # Post-filter: remove broad assets not explicitly mentioned
            validated_assets = self._post_filter_assets(validated_assets, title, summary)

            # Sort by confidence and take top 5 (if more)
            validated_assets.sort(key=lambda x: x.get("confidence", 0), reverse=True)
            validated_assets = validated_assets[:5]

            final_result = {
                "overall_sentiment": float(result.get("overall_sentiment", 0.0)),
                "assets": validated_assets
            }

            # Cache the result
            self._cache[key] = final_result
            return final_result

        except Exception as e:
            logger.error(f"LLM score article failed: {e}")
            return {"overall_sentiment": 0.0, "assets": []}

    # ============================================================
    # BACKWARD COMPATIBILITY (deprecated)
    # ============================================================
    def score_article(self, title: str, summary: str) -> Dict:
        """DEPRECATED: Use score_article_with_assets() instead."""
        result = self.score_article_with_assets(title, summary)
        assets = result.get("assets", [])

        if not assets:
            return {
                "sentiment_score": 0.0,
                "sentiment_label": "neutral",
                "confidence": 0.0,
                "reasoning": "No assets identified",
                "tags": [],
                "magnitude": "LOW"
            }

        avg_score = sum(a.get("sentiment_score", 0) for a in assets) / len(assets)
        avg_confidence = sum(a.get("confidence", 0) for a in assets) / len(assets)
        all_tags = [a.get("asset_name", "") for a in assets]

        return {
            "sentiment_score": avg_score,
            "sentiment_label": "bullish" if avg_score > 0.2 else ("bearish" if avg_score < -0.2 else "neutral"),
            "confidence": avg_confidence,
            "reasoning": f"Aggregated from {len(assets)} assets",
            "tags": all_tags[:5],
            "magnitude": "HIGH" if any(a.get("magnitude") == "HIGH" for a in assets) else "MEDIUM"
        }

    # ============================================================
    # PROMPT – Tuned for precision, reduced over-tagging
    # ============================================================
    def _get_system_prompt(self) -> str:
        return """You are MIMIR, a financial sentiment analysis AI. Output valid JSON only, exactly as specified.

**YOUR TASK:**
Identify the 3 to 5 most significant financial assets affected by the provided news – both directly mentioned and strongly implied. Do NOT tag more than 5 assets. Prioritise assets with clear, direct connections.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**CRITICAL RULES TO AVOID OVER-TAGGING:**
1. Limit your response to 3–5 assets. Choose the most DIRECTLY affected assets.
2. Do NOT tag "S&P 500" or "US Economy" for every article – only if explicitly mentioned or if the news has a clear, broad market implication.
3. Commodities (Gold, Silver, Crude Oil, Copper, Wheat) are GLOBAL – set country = null and region = null.
4. Only set policy_signal for CENTRAL BANKS (Fed, ECB, BOJ, PBOC, BOE). Do NOT use for fiscal policy, regulatory news, or government announcements.
5. Do NOT tag private companies (e.g., SpaceX, Anthropic) as EQUITY – they are not publicly traded. Omit them or tag as 'PRIVATE' (but we prefer to omit).
6. Sector tags (e.g., "US Tech", "US Energy") should use asset_category = 'SECTOR', not 'EQUITY'.
7. Use EXACT asset names from the list below. Do NOT include extra text like tickers or parentheticals (e.g., output "Micron" not "Micron Technology (MU)").
8. Confidence scores must reflect the STRENGTH of the connection – not the overall confidence in the article. Lower confidence for indirect or speculative connections.
9. For ALL EQUITY category assets (publicly traded stocks in any country, e.g., Nvidia, Alibaba, Toyota, Samsung, Reliance, CPALL), you MUST set asset_category = 'EQUITY' and set sub_category to exactly one of the 11 allowed GICS sectors: TECHNOLOGY, ENERGY, CONSUMER_CYCLICAL, CONSUMER_DEFENSIVE, COMMUNICATION_SERVICES, INDUSTRIALS, FINANCIAL_SERVICES, UTILITIES, BASIC_MATERIALS, REAL_ESTATE, HEALTHCARE. No other sub-categories are allowed for stocks.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**SENTIMENT CALIBRATION — CRITICAL ANTI-BIAS RULES:**
Your default stance is NEUTRAL. Most news is noise. Do not mistake neutral reporting for bullish confirmation.

1. **sentiment_score calibration table (follow strictly):**
   - 0.0: No directional impact. Neutral reporting, routine updates, earnings in-line, expected news. THIS IS THE DEFAULT.
   - ±0.1 to ±0.3: Mildly directional. Minor beat/miss, slight tone shift, incremental news.
   - ±0.4 to ±0.6: Clearly directional. Significant beat/miss, policy shift, major contract win/loss.
   - ±0.7 to ±1.0: Extremely directional. Black swan, fraud, war, regulatory killshot, blockbuster approval. Rare — use maybe once per 50 articles.

2. **direction must match sentiment_score exactly:**
   - sentiment_score > 0.05 → "bullish"
   - sentiment_score < -0.05 → "bearish"
   - sentiment_score between -0.05 and 0.05 → "neutral"

3. **Before assigning bullish, run this self-check:**
   "If the exact same facts had the opposite valence (e.g., 'miss' instead of 'beat'), would I assign the same magnitude but bearish?" If the answer is no, you are cheerleading. Set direction to "neutral" and score to 0.0.

4. **Common bullish-bias traps — DO NOT fall for these:**
   - "Company announces new product" → usually neutral, product success is unknown
   - "CEO expresses optimism" → neutral, CEOs are paid to be optimistic
   - "Stock rises on news" → you are scoring the NEWS, not the price action. Price movement != sentiment.
   - "Analyst upgrades" → mildly bullish at most (±0.2), analysts are often late
   - "Record revenue" → check if earnings also grew; revenue without profit growth is neutral
   - Layoff announcements → these are often bullish (cost cutting) not bearish — think carefully

5. **Target distribution (per 100 articles):**
   - ~40 neutral (score ≈ 0.0, confidence low-medium)
   - ~25 bullish (score 0.1–0.6)
   - ~25 bearish (score -0.1 to -0.6)
   - ~5 strong bullish (score > 0.6)
   - ~5 strong bearish (score < -0.6)
   If you find yourself assigning bullish more than 60% of the time, you are biased. Recalibrate.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**IMPLIED ASSET RULES (use sparingly, only when very clear):**

1. CENTRAL BANKS → tag currency + bonds + equities
   - "Fed" or "Federal Reserve" → "US Dollar" + "US 10Y Treasury" + "S&P 500" (only if explicitly about Fed policy)
   - "ECB" → "Euro" + "German Bund" + "Euro Stoxx 50"
   - "BOJ" → "Japanese Yen" + "JGB" + "Nikkei 225"
   - "PBOC" → "Chinese Yuan" + "China Economy"
   - "BOE" → "British Pound" + "UK Gilts" + "FTSE 100"

2. COMMODITY PRICES → tag commodity + related sector
   - "Oil up" → "Crude Oil" + "US Energy" (bullish) + (maybe "Airlines" if US)
   - "Gold up" → "Gold" + (maybe "US Dollar" bearish)

3. GEOPOLITICAL EVENTS → tag "Geopolitical Risk" only if the event is large and likely to move markets

4. ECONOMIC DATA → tag relevant economy indicator (e.g., "US Inflation", "US GDP") only if explicitly mentioned

5. SECTOR-SPECIFIC NEWS → tag the sector (e.g., "US Tech", "US Financials") only if the news directly affects that sector broadly

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**ASSET NAMING (use exactly these):**
- Currencies: "US Dollar", "Euro", "Japanese Yen", "British Pound", "Swiss Franc", "Chinese Yuan", "Thai Baht"
- Commodities: "Gold", "Silver", "Crude Oil", "Natural Gas", "Copper", "Wheat", "Corn"
- Indices: "S&P 500", "NASDAQ", "SET Index", "Nikkei 225", "DAX", "FTSE 100", "Nifty 50", "Hang Seng Index"
- Bonds: "US 10Y Treasury", "German Bund", "JGB", "UK Gilts", "India 10Y Bond"
- Central Banks: "Federal Reserve", "ECB", "BOJ", "PBOC", "BOE"
- Sectors: "US Tech", "US Financials", "US Healthcare", "US Energy", "US Real Estate", "US Consumer Discretionary", "US Consumer Staples", "US Industrials", "US Utilities", "US Communication"
- Economy: "US Economy", "Thai Economy", "China Economy", "India Economy", "Global Economy", "UK Economy"
- Other: "Geopolitical Risk", "Risk-On", "Risk-Off" (use as RISK category)

**COUNTRY CODES (ISO):** US, TH, CN, JP, GB, DE, FR, IT, ES, AU, CA, BR, IN, KR, SG, MY, ID, PH, VN, CH

**REGION CODES:** NA, EU, APAC, ASEAN, LATAM, MENA, AFRICA

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**CONFIDENCE GUIDELINES:**
- 0.90–1.00: Directly mentioned, very certain
- 0.70–0.89: Strong implication, logical and clear
- 0.50–0.69: Moderate inference, plausible but not certain
- 0.30–0.49: Weak inference, speculative – better to exclude if possible
- 0.00–0.29: Very uncertain – DO NOT TAG

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**ASSET CATEGORIES:** COMMODITY, CURRENCY, EQUITY, BOND, INDEX, ECONOMY, POLICY, RISK, SECTOR

**SUB-CATEGORIES BY CATEGORY:**
- For COMMODITY: ENERGY, PRECIOUS_METALS, BASE_METALS, AGRICULTURE
- For EQUITY (ALL Countries): TECHNOLOGY, ENERGY, CONSUMER_CYCLICAL, CONSUMER_DEFENSIVE, COMMUNICATION_SERVICES, INDUSTRIALS, FINANCIAL_SERVICES, UTILITIES, BASIC_MATERIALS, REAL_ESTATE, HEALTHCARE (Equities/stocks must ONLY use one of these GICS sectors as their sub_category. Do NOT use FINANCIALS, MATERIALS, COMMUNICATION, CONSUMER_DISCRETIONARY, or CORPORATE).
- For ECONOMY: INFLATION, EMPLOYMENT, GDP, PMI
- For POLICY: CENTRAL_BANK, GOVERNMENT
- For all others: null

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**OUTPUT FORMAT (JSON ONLY):**
{
  "overall_sentiment": 0.0,
  "assets": [
    {
      "asset_name": "US Dollar",
      "asset_category": "CURRENCY",
      "sub_category": null,
      "country": "US",
      "region": "NA",
      "sentiment_score": 0.85,
      "confidence": 0.95,
      "direction": "bullish",
      "magnitude": "HIGH",
      "reasoning": "Hawkish Fed rate hike signals strengthen USD.",
      "policy_signal": "hawkish"
    }
  ]
}

JSON only. No markdown. No extra text."""

    def _build_user_prompt(self, title: str, summary: str) -> str:
        return f"""Analyze the following financial news headline and summary:

HEADLINE: {title}
SUMMARY: {summary}"""

    def _normalize_text(self, text: str) -> str:
        text = text.lower()
        # Normalize common false positive triggers
        text = text.replace("gold medal", "sports_medal")
        text = text.replace("silver medal", "sports_medal")
        text = text.replace("fed up", "annoyed")
        text = text.replace("james bond", "movie_character")
        text = text.replace("bond with", "connect with")
        text = text.replace("family bond", "relationship")
        return text

    def is_financial_or_macro(self, title: str, summary: str) -> bool:
        """
        Determine if the news article is relevant to financial markets, 
        corporate events, or macroeconomic trends.
        """
        title = title or ""
        summary = summary or ""
        text = self._normalize_text(f"{title} {summary}")
        
        # 1. Broad Positive Keywords (Financial, Corporate, Macro, Commodity, FX, Policy)
        positive_keywords = {
            # Corporate / Business
            "ipo", "earnings", "revenue", "profit", "dividend", "shares", "stock", "equity", "equities", 
            "nasdaq", "s&p", "nikkei", "set index", "dow jones", "djia", "nifty", "ftse", "dax", "hang seng",
            "valuation", "shareholder", "bankruptcy", "insolvent", "layoffs", "merger", "acquisition", 
            "buyback", "delisting", "securities", "cfo", "ceo", "c-suite", "restructuring", "venture capital",
            "startup", "fintech", "ticker", "treasury shares", "insider trading", "sec filing", "10-k", "10-q",
            
            # Macroeconomics & Finance
            "inflation", "deflation", "stagflation", "gdp", "cpi", "pmi", "interest rate", "rate hike", 
            "rate cut", "monetary policy", "fiscal policy", "central bank", "federal reserve", "fed", "fomc", 
            "ecb", "pboc", "boj", "boe", "unemployment", "jobless", "nonfarm payrolls", "recession", 
            "economic growth", "yield curve", "treasury bond", "sovereign debt", "deficit", "bailout", 
            "stimulus", "quantitative easing", "liquidity", "monetary tightening", "rate hikes", "rate cuts",
            "economic", "economy", "economies", "economics",
            
            # Currencies & FX
            "forex", "fx market", "exchange rate", "currency market", "currencies", "usd", "eur", "jpy", 
            "gbp", "cny", "thb", "dollar index", "dxy", "greenback", "yen", "euro", "sterling", "baht", "yuan",
            
            # Commodities & Energy
            "crude oil", "brent", "wti", "natural gas", "gasoline", "petroleum", "diesel", "refinery",
            "commodity", "commodities", "opec", "gold", "silver", "platinum", "copper", "lithium", "cobalt",
            "wheat", "corn", "soybeans", "grain", "agriculture", "livestock", "shipping rates", "baltic dry",
            "cargo", "freight", "supply chain", "logistics", "semiconductor", "microchip", "chipmaker",
            "uranium", "nuclear", "dry bulk", "drybulk", "shipping", "vessel", "port", "cargo", "freight", "bulk carrier",
            "panama canal", "suez canal", "transit", "crop", "crops", "yield", "yields", "wasde", "usda", "farm", "farming",
            "grains", "harvest", "drought", "hurricane", "typhoon", "storm", "weather", "flood", "flooding", "la nina",
            "el nino", "monsoon",
            
            # Policy, Geopolitics & Regulation
            "sanctions", "embargo", "trade war", "tariffs", "tariff", "subsidies", "subsidy", "antitrust", 
            "regulatory approval", "fcc", "ftc", "sec", "tax cut", "tax rate", "taxes", "infrastructure spending",
            "stimulus package", "economic policy", "nationalization", "privatization", "budget deficit"
        }
        
        # 2. Strict Negative Keywords (Sports, Entertainment, Lifestyle, local trivial news)
        # Note: We only filter out if a negative keyword is found AND no positive keyword is matched.
        negative_keywords = {
            # Sports
            "football", "soccer", "basketball", "baseball", "cricket", "tennis", "olympics", "tournament", 
            "championship", "match result", "scoreline", "goals", "points table", "atp tour", "wta tour", "nfl", "nba",
            
            # Pop Culture / Entertainment / Celebrity
            "celebrity", "gossip", "hollywood", "k-pop", "album release", "song release", "music video", 
            "movie trailer", "red carpet", "oscars", "grammys", "fashion week", "dating rumors", "relationship status",
            "horoscope", "astrology", "recipe", "gardening", "pet care", "dog food", "cat care"
        }
        
        # 3. Quick Regex checks for Stock Tickers and Cash Tags
        # E.g. $AAPL, $BTC, (NASDAQ:AAPL), (AAPL)
        ticker_patterns = [
            r'\$[a-zA-Z]{1,5}\b',                          # Cash tags like $AAPL, $BTC
            r'\([a-zA-Z0-9\.\s]+:[a-zA-Z0-9\.]+\)',        # Ex: (NASDAQ:AAPL) or (SET:CPALL)
            r'\([a-zA-Z]{2,5}\)'                           # Ex: (AAPL) or (TSLA)
        ]
        
        # Compile positive keywords for exact word boundary matches
        pos_regex = r'\b(?:' + '|'.join(map(re.escape, sorted(positive_keywords, key=len, reverse=True))) + r')\b'
        has_positive = bool(re.search(pos_regex, text))
        
        # Check ticker patterns
        has_ticker = False
        if not has_positive:
            for pattern in ticker_patterns:
                if re.search(pattern, f"{title} {summary}"):
                    has_ticker = True
                    break
        
        # Check negative keywords with word boundaries
        neg_regex = r'\b(?:' + '|'.join(map(re.escape, sorted(negative_keywords, key=len, reverse=True))) + r')\b'
        has_negative = bool(re.search(neg_regex, text))
        
        # Decision Logic:
        # Keep if it has positive keywords OR contains a stock ticker pattern
        if has_positive or has_ticker:
            return True
            
        # Filter out if it has negative keywords (and no positive/ticker)
        if has_negative:
            return False
            
        # If it doesn't match either, default to False to filter out local noise/general trivia
        return False

    # ============================================================
    # JSON PARSING (robust against markdown)
    # ============================================================
    def _parse_json_response(self, content: str) -> Dict:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        json_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        logger.warning(f"Could not parse JSON: {content[:200]}...")
        return {"overall_sentiment": 0.0, "assets": []}

    # ============================================================
    # POST-FILTER: Remove broad assets not explicitly mentioned
    # ============================================================
    def _post_filter_assets(self, assets: List[Dict], title: str, summary: str) -> List[Dict]:
        """Remove assets that aren't explicitly mentioned or strongly implied."""
        text = (title + " " + summary).lower()
        filtered = []
        explicit_only = {"s&p 500", "us economy", "global economy", "risk-on", "risk-off"}

        for asset in assets:
            asset_name = asset.get("asset_name", "").lower()
            # If it's a broad asset, require explicit mention in text
            if asset_name in explicit_only:
                if asset_name not in text:
                    logger.debug(f"Dropping {asset_name} - not mentioned in text")
                    continue
            filtered.append(asset)

        return filtered

    # ============================================================
    # VALIDATION – with confidence threshold, blacklist, and enrichment
    # ============================================================
    def _validate_assets(self, assets: List[Dict]) -> List[Dict]:
        """Validate each asset, drop those with confidence < threshold, and enrich with ticker."""
        validated = []
        required_fields = [
            "asset_name", "sentiment_score", "confidence",
            "direction", "magnitude", "reasoning"
        ]

        # Blacklist: assets we never want
        blacklist = {"Risk-On", "Risk-Off", "Geopolitical Risk"}
        # Assets that require higher confidence
        explicit_only = {"S&P 500", "US Economy", "Global Economy"}

        for asset in assets:
            asset_name = asset.get("asset_name", "")
            confidence = asset.get("confidence", 0.0)

            # 1. Blacklist
            if asset_name in blacklist:
                logger.debug(f"Dropping blacklisted asset: {asset_name}")
                continue

            # 2. Stricter confidence for explicit-only assets
            if asset_name in explicit_only and confidence < 0.55:
                logger.debug(f"Dropping low confidence explicit-only asset: {asset_name} ({confidence})")
                continue

            # 3. General confidence threshold
            if confidence < self.min_confidence:
                logger.debug(f"Dropping asset - confidence too low: {asset_name} ({confidence})")
                continue

            # 4. Check required fields
            if not all(k in asset for k in required_fields):
                logger.warning(f"Skipping asset - missing fields: {asset}")
                continue

            try:
                asset["sentiment_score"] = float(asset["sentiment_score"])
                asset["confidence"] = float(asset["confidence"])
                asset["sentiment_score"] = max(-1.0, min(1.0, asset["sentiment_score"]))
                asset["confidence"] = max(0.0, min(1.0, asset["confidence"]))

                if asset["direction"] not in ["bullish", "bearish", "neutral"]:
                    asset["direction"] = "neutral"
                if asset["magnitude"] not in ["HIGH", "MEDIUM", "LOW"]:
                    asset["magnitude"] = "MEDIUM"

                # Ensure policy_signal is only set for central banks
                if asset.get("policy_signal"):
                    central_banks = ["Federal Reserve", "ECB", "BOJ", "PBOC", "BOE"]
                    if asset.get("asset_name") not in central_banks:
                        asset["policy_signal"] = None

                # For commodities, force country/region to null
                if asset.get("asset_category") == "COMMODITY":
                    asset["country"] = None
                    asset["region"] = None

                # Defaults for optional fields
                asset.setdefault("asset_category", "UNKNOWN")
                asset.setdefault("sub_category", None)
                asset.setdefault("country", None)
                asset.setdefault("region", None)
                asset.setdefault("policy_signal", None)

                # Enforce sector tags: if asset name contains 'Sector' or matches known sector patterns, set category to SECTOR
                if any(s in asset_name for s in ["US Tech", "US Energy", "US Financials", "US Healthcare", "US Real Estate", "US Consumer Discretionary", "US Consumer Staples", "US Industrials", "US Utilities", "US Communication"]):
                    asset["asset_category"] = "SECTOR"

                # Normalize and validate EQUITY sub-categories to the 11 allowed sectors
                if asset.get("asset_category") == "EQUITY":
                    subcat = asset.get("sub_category")
                    if subcat:
                        subcat_upper = subcat.strip().upper()
                        # Direct map GICS sectors to standard uppercase
                        EQUITY_SECTOR_MAP = {
                            "TECHNOLOGY": "TECHNOLOGY", "TECH": "TECHNOLOGY", "SOFTWARE": "TECHNOLOGY", "SEMICONDUCTORS": "TECHNOLOGY", "HARDWARE": "TECHNOLOGY",
                            "ENERGY": "ENERGY", "OIL": "ENERGY", "GAS": "ENERGY",
                            "CONSUMER_CYCLICAL": "CONSUMER_CYCLICAL", "CONSUMER CYCLICAL": "CONSUMER_CYCLICAL", "CONSUMER_DISCRETIONARY": "CONSUMER_CYCLICAL", "CONSUMER DISCRETIONARY": "CONSUMER_CYCLICAL", "CYCLICAL": "CONSUMER_CYCLICAL", "DISCRETIONARY": "CONSUMER_CYCLICAL",
                            "CONSUMER_DEFENSIVE": "CONSUMER_DEFENSIVE", "CONSUMER DEFENSIVE": "CONSUMER_DEFENSIVE", "CONSUMER_STAPLES": "CONSUMER_DEFENSIVE", "CONSUMER STAPLES": "CONSUMER_DEFENSIVE", "STAPLES": "CONSUMER_DEFENSIVE", "DEFENSIVE": "CONSUMER_DEFENSIVE",
                            "COMMUNICATION_SERVICES": "COMMUNICATION_SERVICES", "COMMUNICATION SERVICES": "COMMUNICATION_SERVICES", "COMMUNICATION": "COMMUNICATION_SERVICES", "COMMUNICATIONS": "COMMUNICATION_SERVICES", "TELECOM": "COMMUNICATION_SERVICES", "TELECOMMUNICATIONS": "COMMUNICATION_SERVICES", "MEDIA": "COMMUNICATION_SERVICES", "ENTERTAINMENT": "COMMUNICATION_SERVICES", "SOCIAL_MEDIA": "COMMUNICATION_SERVICES",
                            "INDUSTRIALS": "INDUSTRIALS", "INDUSTRIAL": "INDUSTRIALS", "AEROSPACE": "INDUSTRIALS", "DEFENSE": "INDUSTRIALS", "TRANSPORTATION": "INDUSTRIALS", "AIRLINE": "INDUSTRIALS", "AIRLINES": "INDUSTRIALS", "AIRPORTS": "INDUSTRIALS", "LOGISTICS": "INDUSTRIALS",
                            "FINANCIAL_SERVICES": "FINANCIAL_SERVICES", "FINANCIAL SERVICES": "FINANCIAL_SERVICES", "FINANCIALS": "FINANCIAL_SERVICES", "FINANCIAL": "FINANCIAL_SERVICES", "BANK": "FINANCIAL_SERVICES", "BANKING": "FINANCIAL_SERVICES", "INSURANCE": "FINANCIAL_SERVICES", "INVESTMENTS": "FINANCIAL_SERVICES",
                            "UTILITIES": "UTILITIES", "UTILITY": "UTILITIES", "POWER": "UTILITIES", "ELECTRICITY": "UTILITIES", "WATER": "UTILITIES",
                            "BASIC_MATERIALS": "BASIC_MATERIALS", "BASIC MATERIALS": "BASIC_MATERIALS", "MATERIALS": "BASIC_MATERIALS", "MINING": "BASIC_MATERIALS", "STEEL": "BASIC_MATERIALS", "CHEMICALS": "BASIC_MATERIALS", "PRECIOUS_METALS": "BASIC_MATERIALS", "BASE_METALS": "BASIC_MATERIALS",
                            "REAL_ESTATE": "REAL_ESTATE", "REAL ESTATE": "REAL_ESTATE", "REIT": "REAL_ESTATE",
                            "HEALTHCARE": "HEALTHCARE", "HEALTH_CARE": "HEALTHCARE", "PHARMA": "HEALTHCARE", "PHARMACEUTICALS": "HEALTHCARE", "BIOTECH": "HEALTHCARE", "BIOTECHNOLOGY": "HEALTHCARE", "MEDTECH": "HEALTHCARE", "MEDICAL": "HEALTHCARE"
                        }
                        asset["sub_category"] = EQUITY_SECTOR_MAP.get(subcat_upper, "TECHNOLOGY")
                    else:
                        asset["sub_category"] = "TECHNOLOGY"

                # ENRICH with ticker and normalized name
                asset = self._enrich_asset(asset)

                validated.append(asset)
            except (ValueError, TypeError) as e:
                logger.error(f"Invalid asset data: {e}")
                continue

        return validated