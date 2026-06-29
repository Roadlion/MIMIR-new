# backend/app/pipeline/spillover_engine.py
"""
Propagates sentiment from direct article impacts to related assets
via the RelationshipGraph. Runs inline in sentiment_processor.py.
"""
import logging
from typing import List, Dict, Tuple, Optional
from ..sentiment.relationship_graph import get_relationship_graph
from ..sentiment.asset_mapper import resolve_ticker, resolve_country_code

logger = logging.getLogger(__name__)

# Decay overrides by spillover type — applied ON TOP of the relationship's
# own decay_factor (multiplied). The relationship decay is the max allowed;
# this is an additional safety cap.
TYPE_DECAY_CAP = {
    "sector_to_constituent": 0.50,
    "constituent_to_sector": 0.30,
    "macro_to_asset": 0.25,
    "central_bank_to_asset": 0.35,
    "thematic_to_asset": 0.20,
    "default": 0.15,
}

# Minimum absolute score to create a spillover impact
NOISE_FLOOR = 0.05


class SpilloverEngine:
    """Computes spillover impacts for a single article's direct impacts."""

    def __init__(self):
        self.graph = get_relationship_graph()

    def run(
        self,
        article_id: int,
        direct_impacts: List[Dict],
    ) -> List[Tuple]:
        """
        For each direct impact, find relationship graph targets and
        compute spillover scores.

        Args:
            article_id: The raw_articles.id
            direct_impacts: List of dicts with keys:
                asset_name, asset_category, sentiment_score, confidence,
                ticker, country, region, sub_category

        Returns:
            List of tuples ready for INSERT into mimir_sentiment_impacts:
            (article_id, asset_name, asset_category, sub_category,
             country, region, sentiment_score, confidence, direction,
             magnitude, reasoning, ticker, policy_signal,
             is_spillover, spillover_source_article_id, spillover_source_asset)
        """
        spillovers: List[Tuple] = []
        # Track which (asset_name) targets we already have direct impacts for
        direct_keys = self._direct_impact_keys(direct_impacts)

        for impact in direct_impacts:
            source_keys = self._impact_to_source_keys(impact)
            for source_key in source_keys:
                targets = self.graph.get_spillover_targets(source_key)
                for target_type, target_key, rel_decay in targets:
                    # Resolve target identity
                    target_asset_name = target_key
                    target_ticker = None
                    if target_type == "ticker":
                        target_ticker = target_key
                        # ponytail: use ticker as asset_name if no name mapping exists
                        target_asset_name = target_key
                    elif target_type == "asset_name":
                        target_ticker, _ = resolve_ticker(target_key)
                    else:
                        # 'sector', 'economy' etc. — skip, these are source keys not targets
                        continue

                    # Dedup: skip if already directly tagged in this article
                    dedup_key = target_asset_name.upper()
                    if dedup_key in direct_keys:
                        continue

                    # Compute spillover score
                    cap = self._resolve_cap(impact)
                    effective_decay = min(rel_decay, cap)
                    spill_score = impact.get("sentiment_score", 0.0) * effective_decay

                    if abs(spill_score) < NOISE_FLOOR:
                        continue

                    # Confidence: inherit from source, discounted
                    spill_confidence = round(
                        impact.get("confidence", 0.5) * 0.8, 3
                    )

                    # Direction from score
                    if spill_score > 0.05:
                        direction = "bullish"
                    elif spill_score < -0.05:
                        direction = "bearish"
                    else:
                        direction = "neutral"

                    # Magnitude
                    magnitude = "LOW" if abs(spill_score) < 0.15 else "MEDIUM"

                    reasoning = (
                        f"Spillover from {impact.get('asset_name', source_key)} "
                        f"(score={impact.get('sentiment_score', 0):.3f}, "
                        f"decay={effective_decay:.2f})"
                    )

                    spillovers.append((
                        article_id,
                        target_asset_name,
                        self._infer_category(impact, target_asset_name),
                        None,  # sub_category
                        impact.get("country"),
                        impact.get("region"),
                        round(spill_score, 4),
                        spill_confidence,
                        direction,
                        magnitude,
                        reasoning,
                        target_ticker,
                        None,  # policy_signal
                        True,  # is_spillover
                        article_id,  # spillover_source_article_id
                        impact.get("asset_name", source_key),  # spillover_source_asset
                    ))

                    # Track dedup across all spillovers within this article
                    direct_keys.add(dedup_key)

        logger.debug(
            "Article %d: %d direct → %d spillover impacts",
            article_id, len(direct_impacts), len(spillovers),
        )
        return spillovers

    # ------------------------------------------------------------------
    def _direct_impact_keys(self, impacts: List[Dict]) -> set:
        """Build set of (uppercase asset_name) already directly tagged."""
        keys = set()
        for imp in impacts:
            name = imp.get("asset_name", "").strip().upper()
            ticker = imp.get("ticker", "")
            if name:
                keys.add(name)
            if ticker:
                keys.add(ticker.upper())
        return keys

    def _impact_to_source_keys(self, impact: Dict) -> List[str]:
        """
        Convert an impact dict into one or more relationship graph source keys.
        Tries: ticker, asset_name, normalized sector key, asset_name as-is.
        """
        keys = []
        ticker = impact.get("ticker", "")
        asset_name = impact.get("asset_name", "")
        category = impact.get("asset_category", "")
        sub_cat = impact.get("sub_category", "")
        country = impact.get("country", "")

        # 1. Ticker (for constituent→sector reverse spillover)
        if ticker:
            keys.append(ticker)

        # 2. Asset name as-is (for macro sources like "Federal Reserve", "US Economy")
        if asset_name:
            keys.append(asset_name)

        # 3. Category-specific keys
        if category == "SECTOR" or category == "EQUITY":
            # Build country.sector key
            sector_name = sub_cat or asset_name
            # Normalize via SECTOR_NORM_MAP if possible
            try:
                from ..routers.sentiment import SECTOR_NORM_MAP
                sector_canon = SECTOR_NORM_MAP.get(sector_name.upper(), sector_name)
            except Exception:
                sector_canon = sector_name

            if country:
                keys.append(f"{country}.{sector_canon}")
            # Also try US fallback
            keys.append(f"US.{sector_canon}")

        elif category == "POLICY" and sub_cat == "CENTRAL_BANK":
            # Asset name already handled above (e.g., "Federal Reserve")
            pass

        elif category == "ECONOMY":
            # Asset name already handled (e.g., "US Economy")
            pass

        elif category == "RISK":
            # Asset name already handled (e.g., "Geopolitical Risk")
            pass

        return keys

    def _resolve_cap(self, impact: Dict) -> float:
        cat = impact.get("asset_category", "")
        sub = impact.get("sub_category", "")
        if cat == "SECTOR" or cat == "EQUITY":
            return TYPE_DECAY_CAP["sector_to_constituent"]
        elif cat == "ECONOMY":
            return TYPE_DECAY_CAP["macro_to_asset"]
        elif cat == "POLICY" and sub == "CENTRAL_BANK":
            return TYPE_DECAY_CAP["central_bank_to_asset"]
        elif cat == "RISK":
            return TYPE_DECAY_CAP["thematic_to_asset"]
        return TYPE_DECAY_CAP["default"]

    def _infer_category(self, impact: Dict, target_name: str) -> str:
        """Best-effort category inference for spillover targets."""
        # If source is a sector, target is likely EQUITY
        src_cat = impact.get("asset_category", "")
        if src_cat in ("SECTOR", "EQUITY"):
            return "EQUITY"
        # ponytail: defaults to EQUITY; DB cleanup normalizes later
        return "EQUITY"
