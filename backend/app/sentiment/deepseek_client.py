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

        # For commodities, ensure country/region are null (already handled in validation)
        return asset

    # ============================================================
    # MAIN ENTRY: Per-asset sentiment with strict rules
    # ============================================================
    def score_article_with_assets(self, title: str, summary: str) -> Dict:
        """
        Send article to DeepSeek for multi-asset sentiment scoring.
        Returns a dict with 'overall_sentiment' and list of 'assets',
        each asset now includes a 'ticker' field.
        """
        key = (title, summary)
        if key in self._cache:
            logger.debug("Returning cached result for article")
            return self._cache[key]

        prompt = self._build_asset_prompt(title, summary)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are MIMIR, a financial sentiment analysis AI. Output valid JSON only, exactly as specified."
                },
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"}
        }

        max_retries = 3
        retry_delay = 2

        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=60
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                # Log token usage if available
                usage = data.get("usage", {})
                logger.info(f"Token usage: {usage}")

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

            except requests.exceptions.Timeout:
                delay = retry_delay * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(f"Timeout (attempt {attempt+1}/{max_retries}). Retrying in {delay:.2f}s...")
                time.sleep(delay)
            except Exception as e:
                logger.error(f"Failed: {e}")
                if attempt == max_retries - 1:
                    return {"overall_sentiment": 0.0, "assets": []}
                time.sleep(retry_delay)

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
    def _build_asset_prompt(self, title: str, summary: str) -> str:
        return f"""
Analyze the following financial news headline and summary.

HEADLINE: {title}
SUMMARY: {summary}

**YOUR TASK:**
Identify the 3 to 5 most significant financial assets affected by this news – both directly mentioned and strongly implied. Do NOT tag more than 5 assets. Prioritise assets with clear, direct connections.

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

**SUB-CATEGORIES:** ENERGY, PRECIOUS_METALS, BASE_METALS, AGRICULTURE, TECHNOLOGY, FINANCIALS, HEALTHCARE, CONSUMER_CYCLICAL, CONSUMER_DEFENSIVE, INDUSTRIALS, MATERIALS, REAL_ESTATE, UTILITIES, COMMUNICATION, INFLATION, EMPLOYMENT, GDP, PMI, CENTRAL_BANK, GOVERNMENT, CORPORATE

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**OUTPUT FORMAT (JSON ONLY):**
{{
  "overall_sentiment": 0.0,
  "assets": [
    {{
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
    }}
  ]
}}

JSON only. No markdown. No extra text.
"""

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

                # ENRICH with ticker and normalized name
                asset = self._enrich_asset(asset)

                validated.append(asset)
            except (ValueError, TypeError) as e:
                logger.error(f"Invalid asset data: {e}")
                continue

        return validated