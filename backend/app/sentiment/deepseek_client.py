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
        # Truncate summary to 400 chars to save tokens
        summary = (summary or "")[:400]
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

    def score_articles_batch(self, articles: List[Dict]) -> Dict[int, Dict]:
        """
        Score a batch of articles (up to 5) in a single LLM API call.
        Each item in `articles` must be a dict with keys: 'id', 'title', 'summary'.
        Returns a dict mapping article_id -> {"overall_sentiment": float, "assets": List[Dict]}
        """
        if not articles:
            return {}

        results = {}
        articles_to_query = []

        for art in articles:
            aid = art.get("id")
            title = art.get("title", "")
            summary = (art.get("summary") or "")[:400]
            key = (title, summary)

            if key in self._cache:
                results[aid] = self._cache[key]
            else:
                # Pre-filter using Python regex locally before adding to query batch
                if not art.get("force_relevance", False) and not self.is_financial_or_macro(title, summary):
                    empty_res = {"overall_sentiment": 0.0, "assets": []}
                    self._cache[key] = empty_res
                    results[aid] = empty_res
                else:
                    articles_to_query.append({
                        "id": aid,
                        "title": title,
                        "summary": summary
                    })

        if not articles_to_query:
            return results

        system_prompt = self._get_batch_system_prompt()
        
        items_text = []
        for art in articles_to_query:
            items_text.append(f"ARTICLE ID: {art['id']}\nTITLE: {art['title']}\nSUMMARY: {art['summary']}\n---")

        user_prompt = f"Analyze the following {len(articles_to_query)} financial articles:\n\n" + "\n".join(items_text)

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

            raw_data = self._parse_json_response(content)
            res_list = raw_data.get("results", [])
            if isinstance(raw_data, list):
                res_list = raw_data
            elif not isinstance(res_list, list):
                # Fallback check for alternate keys
                for v in raw_data.values():
                    if isinstance(v, list):
                        res_list = v
                        break

            res_map = {}
            for item in res_list:
                if isinstance(item, dict) and "article_id" in item:
                    try:
                        res_map[int(item["article_id"])] = item
                    except (ValueError, TypeError):
                        pass

            for art in articles_to_query:
                aid = art["id"]
                title = art["title"]
                summary = art["summary"]
                key = (title, summary)

                item_res = res_map.get(aid, {})
                assets = item_res.get("assets", [])
                if not isinstance(assets, list):
                    assets = []

                validated = self._validate_assets(assets)
                validated = self._post_filter_assets(validated, title, summary)
                validated.sort(key=lambda x: x.get("confidence", 0), reverse=True)
                validated = validated[:5]

                final_res = {
                    "overall_sentiment": float(item_res.get("overall_sentiment", 0.0)),
                    "assets": validated
                }
                self._cache[key] = final_res
                results[aid] = final_res

        except Exception as e:
            logger.error(f"LLM score_articles_batch failed: {e}")
            for art in articles_to_query:
                aid = art["id"]
                if aid not in results:
                    results[aid] = {"overall_sentiment": 0.0, "assets": []}

        return results

    def score_social_chatter(self, ticker: str, asset_name: str, summary_text: str) -> Dict:
        """
        Lightweight, low-token sentiment scoring for pre-tagged social posts/chatter.
        Uses ~150-token prompt instead of full multi-asset detection.
        """
        summary_text = (summary_text or "")[:1500]
        system_prompt = "You are MIMIR, a financial sentiment AI. Score market sentiment for a known asset from social media chatter. Output valid JSON only."
        user_prompt = f"""Asset: {asset_name} (Ticker: {ticker})
Social Chatter Summary:
{summary_text}

Determine crowd sentiment score (-1.0 to 1.0) and confidence for {ticker}.
JSON schema:
{{
  "sentiment_score": 0.0,
  "confidence": 0.8,
  "direction": "neutral",
  "magnitude": "MEDIUM",
  "reasoning": "1 sentence crowd summary."
}}"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        try:
            content = send_chat_completion(
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
                timeout=45
            )
            raw = self._parse_json_response(content)
            score = float(raw.get("sentiment_score", 0.0))
            score = max(-1.0, min(1.0, score))
            conf = float(raw.get("confidence", 0.8))
            conf = max(0.0, min(1.0, conf))
            direction = raw.get("direction", "neutral")
            if direction not in ["bullish", "bearish", "neutral"]:
                direction = "neutral"
            mag = raw.get("magnitude", "MEDIUM")
            if mag not in ["HIGH", "MEDIUM", "LOW"]:
                mag = "MEDIUM"

            single_asset = {
                "asset_name": asset_name,
                "ticker": ticker,
                "asset_category": "EQUITY",
                "sub_category": None,
                "country": None,
                "region": None,
                "sentiment_score": score,
                "confidence": conf,
                "direction": direction,
                "magnitude": mag,
                "reasoning": raw.get("reasoning", "Social chatter aggregate"),
                "policy_signal": None
            }
            return {
                "overall_sentiment": score,
                "assets": [single_asset]
            }
        except Exception as e:
            logger.error(f"score_social_chatter failed for {ticker}: {e}")
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
        return """You are MIMIR, a financial sentiment analysis AI. Output JSON only.

Identify 1 to 5 key financial assets affected by the news (directly mentioned or strongly implied).
Default stance is NEUTRAL (score 0.0). Most news is noise. Avoid bullish bias.

RULES:
1. Max 5 assets per article. Choose most direct connections.
2. Do NOT tag broad assets ("S&P 500", "US Economy") unless explicitly mentioned.
3. Commodities (Gold, Crude Oil, etc.): set country=null and region=null.
4. policy_signal: ONLY for Central Banks ("Federal Reserve", "ECB", "BOJ", "PBOC", "BOE"). Values: hawkish|bullish|dovish|bearish|neutral|null.
5. EQUITY assets: asset_category MUST be 'EQUITY'. sub_category MUST be one of 11 allowed GICS sectors: TECHNOLOGY, ENERGY, CONSUMER_CYCLICAL, CONSUMER_DEFENSIVE, COMMUNICATION_SERVICES, INDUSTRIALS, FINANCIAL_SERVICES, UTILITIES, BASIC_MATERIALS, REAL_ESTATE, HEALTHCARE.
6. sentiment_score range [-1.0 to 1.0]. 0.0 = neutral/in-line. direction MUST match score (>0.05: bullish, <-0.05: bearish, else neutral).
7. Categories: COMMODITY, CURRENCY, EQUITY, BOND, INDEX, ECONOMY, POLICY, RISK, SECTOR.

OUTPUT JSON SCHEMA:
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
}"""

    def _get_batch_system_prompt(self) -> str:
        return """You are MIMIR, a financial sentiment analysis AI. Output JSON only.

Analyze the array of news items provided. For EACH article, identify 1 to 5 key financial assets affected.
Default stance is NEUTRAL (score 0.0). Most news is noise. Avoid bullish bias.

RULES:
1. Max 5 assets per article. Choose most direct connections.
2. Commodities: set country=null and region=null.
3. policy_signal: ONLY for Central Banks ("Federal Reserve", "ECB", "BOJ", "PBOC", "BOE").
4. EQUITY assets: asset_category MUST be 'EQUITY'. sub_category MUST be one of 11 allowed GICS sectors: TECHNOLOGY, ENERGY, CONSUMER_CYCLICAL, CONSUMER_DEFENSIVE, COMMUNICATION_SERVICES, INDUSTRIALS, FINANCIAL_SERVICES, UTILITIES, BASIC_MATERIALS, REAL_ESTATE, HEALTHCARE.
5. sentiment_score range [-1.0 to 1.0]. 0.0 = neutral. direction MUST match score (>0.05: bullish, <-0.05: bearish, else neutral).
6. Categories: COMMODITY, CURRENCY, EQUITY, BOND, INDEX, ECONOMY, POLICY, RISK, SECTOR.

OUTPUT JSON SCHEMA:
{
  "results": [
    {
      "article_id": 123,
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
  ]
}"""

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