# backend/app/sentiment/thematic_detector.py
"""
Regex-based thematic event detection.
Scans article title+summary for macro/thematic keywords and
generates spillover impacts for thematically-related assets.

No extra LLM call — regex is free and fast.
"""
import re
import logging
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)

# ============================================================================
# Thematic Patterns
# ============================================================================
# Each entry: { "theme", "decay", "direction_override", "affected" }
# direction_override: None = follow article sentiment, "bullish"/"bearish" = fixed

THEMATIC_PATTERNS = [
    # --- Geopolitical / War ---
    {
        "patterns": [
            r"\b(?:war|military\s+action|invasion|conflict|hostilities|ceasefire|truce)\b",
            r"\b(?:middle\s+east|iran|israel|gaza|hezbollah|houthi|hormuz|strait\s+of\s+hormuz)\b",
        ],
        "theme": "war_middle_east",
        "decay": 0.20,
        "direction_override": None,  # follows article sentiment
        "affected": ["LMT", "RTX", "GD", "NOC", "XLE", "GLD", "BDRY"],
    },
    # --- Trade War / Tariffs ---
    {
        "patterns": [
            r"\b(?:trade\s+war|tariff|tariffs|trade\s+barrier|protectionist|import\s+ban)\b",
        ],
        "theme": "trade_war",
        "decay": 0.15,
        "direction_override": None,
        "affected": ["SPY", "EEM", "XLY", "XLI", "FXI"],
    },
    # --- Oil Supply Shock ---
    {
        "patterns": [
            r"\b(?:oil\s+price\s+(?:surge|spike|crash|plunge)|crude\s+(?:surge|spike|crash|plunge)|opec(?:\+)?\s*(?:cuts?|boost|decision)|oil\s+(?:supply|production)\s+(?:cut|disruption|shock))\b",
        ],
        "theme": "oil_shock",
        "decay": 0.20,
        "direction_override": None,
        "affected": ["XLE", "XOP", "USO", "GLD", "BDRY"],
    },
    # --- Agriculture / Weather Shock ---
    {
        "patterns": [
            r"\b(?:drought|flood|hurricane|typhoon|crop\s+failure|food\s+crisis|el\s+niño|la\s+niña|heatwave)\b",
        ],
        "theme": "agriculture_shock",
        "decay": 0.15,
        "direction_override": None,
        "affected": ["WEAT", "CORN", "SOYB", "MOO", "DBA"],
    },
    # --- Regulatory / Antitrust ---
    {
        "patterns": [
            r"\b(?:regulatory\s+crackdown|antitrust|DOJ\s+(?:sues?|investigation)|FTC\s+(?:sues?|investigation)|SEC\s+(?:investigation|charges|fine))\b",
        ],
        "theme": "regulatory_action",
        "decay": 0.10,
        "direction_override": "bearish",  # regulatory action is almost always negative
        "affected": ["SPY", "XLK", "XLC"],
    },
    # --- Health Crisis / Pandemic ---
    {
        "patterns": [
            r"\b(?:pandemic|outbreak|epidemic|lockdown|quarantine|health\s+crisis|virus\s+outbreak)\b",
        ],
        "theme": "health_crisis",
        "decay": 0.15,
        "direction_override": None,
        "affected": ["XLV", "PFE", "MRNA", "SPY"],
    },
    # --- Monetary Policy Shift ---
    {
        "patterns": [
            r"\b(?:hawkish\s+(?:pivot|turn|shift|stance)|dovish\s+(?:pivot|turn|shift|stance)|rate\s+hike|rate\s+cut|interest\s+rate\s+(?:hike|cut|increase|decrease)|tightening\s+cycle|easing\s+cycle)\b",
        ],
        "theme": "monetary_policy_shift",
        "decay": 0.20,
        "direction_override": None,
        "affected": ["TLT", "DX-Y.NYB", "SPY", "GLD", "IEF"],
    },
    # --- Banking Crisis ---
    {
        "patterns": [
            r"\b(?:bank\s+(?:failure|run|collapse|bailout)|banking\s+crisis|credit\s+crunch|financial\s+contagion)\b",
        ],
        "theme": "banking_crisis",
        "decay": 0.20,
        "direction_override": "bearish",
        "affected": ["XLF", "KBE", "SPY", "GLD"],
    },
    # --- Tech / AI Breakthrough ---
    {
        "patterns": [
            r"\b(?:AI\s+breakthrough|LLM|foundation\s+model|GPT-?\d|artificial\s+intelligence\s+(?:breakthrough|revolution)|generative\s+AI)\b",
        ],
        "theme": "ai_breakthrough",
        "decay": 0.18,
        "direction_override": "bullish",
        "affected": ["NVDA", "XLK", "QQQ", "SMH", "AMD"],
    },
    # --- Semiconductor / Chip News ---
    {
        "patterns": [
            r"\b(?:chip\s+(?:ban|shortage|export\s+control|sanction)|semiconductor\s+(?:ban|shortage|restriction))\b",
        ],
        "theme": "chip_restriction",
        "decay": 0.15,
        "direction_override": "bearish",
        "affected": ["NVDA", "AMD", "INTC", "SMH", "SOXX"],
    },
]


class ThematicDetector:
    """Detects themes in article text and generates spillover impacts."""

    # Compiled patterns built lazily
    _compiled = False

    def __init__(self):
        if not ThematicDetector._compiled:
            for entry in THEMATIC_PATTERNS:
                entry["_regexes"] = [
                    re.compile(p, re.IGNORECASE) for p in entry["patterns"]
                ]
            ThematicDetector._compiled = True

    def detect_themes(self, title: str, summary: str) -> List[Dict]:
        """
        Scan article text for thematic keywords.
        Returns list of {theme, decay, direction_override, affected_tickers, matched}.
        """
        text = f"{title or ''} {summary or ''}".lower()
        if len(text) < 20:
            return []

        hits = []
        for entry in THEMATIC_PATTERNS:
            matched = None
            for regex in entry["_regexes"]:
                m = regex.search(text)
                if m:
                    matched = m.group(0)
                    break

            if matched:
                hits.append({
                    "theme": entry["theme"],
                    "decay": entry["decay"],
                    "direction_override": entry.get("direction_override"),
                    "affected_tickers": list(entry["affected"]),
                    "matched_keyword": matched,
                })

        return hits

    def compute_spillovers(
        self,
        article_id: int,
        direct_impacts: List[Dict],
        title: str,
        summary: str,
    ) -> List[Tuple]:
        """
        Compute thematic spillover impact tuples.

        Direction logic:
        - If direction_override is set → use it regardless of article tone
        - Otherwise → follow the article's average direct sentiment

        Returns same tuple format as SpilloverEngine.run().
        """
        themes = self.detect_themes(title, summary)
        if not themes:
            return []

        # Collect tickers already directly tagged (to dedup)
        direct_tickers = set()
        for imp in direct_impacts:
            t = imp.get("ticker", "")
            if t:
                direct_tickers.add(t.upper())

        # Determine article sentiment direction
        article_sent = self._article_sentiment(direct_impacts)

        spillovers = []
        seen = set()  # dedup within thematic spillovers

        for theme in themes:
            for ticker in theme["affected_tickers"]:
                if ticker.upper() in direct_tickers:
                    continue
                dedup = (article_id, ticker.upper())
                if dedup in seen:
                    continue
                seen.add(dedup)

                # Determine score and direction
                if theme["direction_override"] == "bullish":
                    score = abs(article_sent) * theme["decay"]
                    if score < 0.05:
                        score = 0.10 * theme["decay"]  # minimum positive
                    direction = "bullish"
                elif theme["direction_override"] == "bearish":
                    score = -abs(article_sent) * theme["decay"]
                    if score > -0.05:
                        score = -0.10 * theme["decay"]
                    direction = "bearish"
                else:
                    # Follow article sentiment
                    score = article_sent * theme["decay"]
                    if score > 0.05:
                        direction = "bullish"
                    elif score < -0.05:
                        direction = "bearish"
                    else:
                        direction = "neutral"

                if abs(score) < 0.03:
                    continue

                magnitude = "LOW" if abs(score) < 0.15 else "MEDIUM"

                spillovers.append((
                    article_id,
                    ticker,  # asset_name
                    "EQUITY",  # asset_category
                    None,  # sub_category
                    None,  # country
                    None,  # region
                    round(score, 4),
                    0.30,  # confidence — thematic is inherently lower confidence
                    direction,
                    magnitude,
                    f"Thematic spillover: {theme['theme']} (matched: '{theme['matched_keyword']}')",
                    ticker,  # ticker
                    None,  # policy_signal
                    True,  # is_spillover
                    article_id,  # spillover_source_article_id
                    f"theme:{theme['theme']}",  # spillover_source_asset
                ))

        if spillovers:
            logger.debug(
                "Article %d: %d thematic spillovers from '%s'",
                article_id, len(spillovers), title[:60] if title else "",
            )

        return spillovers

    def _article_sentiment(self, direct_impacts: List[Dict]) -> float:
        """Confidence-weighted average sentiment of direct impacts."""
        if not direct_impacts:
            return 0.0
        total_w = 0.0
        total_s = 0.0
        for imp in direct_impacts:
            conf = imp.get("confidence", 0.5)
            score = imp.get("sentiment_score", 0.0)
            total_w += conf
            total_s += score * conf
        return total_s / total_w if total_w > 0 else 0.0
