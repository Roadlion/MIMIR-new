# backend/app/pipeline/spillover_engine.py
"""
MIMIR Institutional Spillover Engine (v2)
=========================================
Propagates sentiment from direct article impacts to economically coupled assets
via the RelationshipGraph.

Institutional Capabilities:
1. Polarity-Aware Arithmetic: Correctly inverts sentiment on Competitor/Rival relationships (-1.0).
2. Dynamic Lead-Lag Activation: Uses edge.lead_lag_hours instead of hardcoded 48h delay.
3. Revenue Exposure Weighting: Scales impact by supplier's customer concentration percentage.
4. Operating Leverage Multiplier (OLM): Amplifies mega-cap contracts on sub-tier suppliers.
5. 2-Hop Multi-Tier BFS Traversal: Cascades high-magnitude catalysts to upstream tooling & equipment.
6. Zero Noise Pollution: Strictly queries verified active edges (bypasses quarantined co-occurrence noise).
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional, Any

from ..sentiment.relationship_graph import get_relationship_graph, RelationshipEdge
from ..sentiment.asset_mapper import resolve_ticker, resolve_country_code

logger = logging.getLogger(__name__)

# Safety caps on decay factors by category
TYPE_DECAY_CAP = {
    "supply_chain": 0.85,
    "sector_to_constituent": 0.50,
    "constituent_to_sector": 0.30,
    "macro_to_asset": 0.30,
    "central_bank_to_asset": 0.35,
    "thematic_to_asset": 0.25,
    "default": 0.40,
}

# Minimum absolute score to persist a spillover impact
NOISE_FLOOR = 0.05


class SpilloverEngine:
    """Computes high-conviction, typed, and polarized spillover impacts."""

    def __init__(self):
        self.graph = get_relationship_graph()

    def run(
        self,
        article_id: int,
        direct_impacts: List[Dict],
        published_ts: Optional[datetime] = None,
    ) -> List[Tuple]:
        """
        For each direct impact, traverses active edges in the RelationshipGraph
        and computes polarized, diffusion-aware spillover scores.
        """
        if published_ts is None:
            published_ts = datetime.now(timezone.utc)

        best_spillovers: Dict[str, Dict] = {}
        direct_keys = self._direct_impact_keys(direct_impacts)
        first_hop_keys = set()

        for impact in direct_impacts:
            source_keys = self._impact_to_source_keys(impact)
            source_sent = float(impact.get("sentiment_score", 0.0))
            source_conf = float(impact.get("confidence", 0.5))
            source_name = impact.get("asset_name", "") or (source_keys[0] if source_keys else "Unknown Asset")
            cap = self._resolve_cap(impact)

            for source_key in source_keys:
                skip_list = set(self.graph.get_skip_list(source_key))
                edges = self.graph.get_spillover_edges(source_key)

                for edge in edges:
                    target_key = edge.target_key
                    target_type = edge.target_type
                    rel_type = edge.relationship_type
                    polarity = edge.polarity
                    lead_lag_hours = edge.lead_lag_hours
                    rev_pct = edge.revenue_exposure_pct
                    rationale = edge.economic_rationale

                    target_ticker = target_key if target_type == "ticker" else None
                    target_asset_name = target_key
                    if target_type == "asset_name":
                        target_ticker, _ = resolve_ticker(target_key)

                    # Skip frontline ticker (Trade the ripple, not the splash)
                    if target_ticker and target_ticker.upper() in skip_list:
                        continue

                    dedup_key = target_asset_name.upper()
                    if dedup_key in direct_keys:
                        continue

                    first_hop_keys.add(dedup_key)

                    # 1. Effective Decay with Category Cap
                    effective_decay = min(edge.decay_factor, cap)

                    # 2. Revenue Exposure Scaling (10% standard = 1.0x, 60% concentration = 1.5x)
                    rev_weight = max(0.70, min(1.50, 0.50 + (rev_pct / 20.0)))

                    # 3. Operating Leverage Multiplier (boost sub-tier infrastructure & equipment)
                    olm = 1.20 if rel_type in ("INFRA_COOLING_POWER", "EQUIPMENT_RAW_MAT") else 1.00

                    # 4. Polarity-Aware Sentiment Multiplication
                    # If polarity == -1.0 (Competitor), negative news on source inverts to POSITIVE on target
                    spill_score = round(source_sent * effective_decay * polarity * rev_weight * olm, 4)

                    if abs(spill_score) < NOISE_FLOOR:
                        continue

                    # 5. Dynamic Lead-Lag Activation Date
                    activation_date = published_ts + timedelta(hours=lead_lag_hours)
                    spill_confidence = round(min(0.95, source_conf * 0.85), 3)

                    # Direction based on signed spill_score
                    if spill_score > 0.05:
                        direction = "bullish"
                    elif spill_score < -0.05:
                        direction = "bearish"
                    else:
                        direction = "neutral"

                    # Magnitude
                    abs_score = abs(spill_score)
                    magnitude = "HIGH" if abs_score >= 0.40 else ("MEDIUM" if abs_score >= 0.18 else "LOW")

                    # Structured Institutional Reasoning
                    if polarity < 0:
                        reasoning = (
                            f"[COMPETITOR GAIN] Rival spillover from {source_name} "
                            f"(source={source_sent:+.2f}, inverted by competitor polarity -1.0, lag={lead_lag_hours}h). "
                            f"{rationale}"
                        )
                    else:
                        reasoning = (
                            f"[{rel_type}] Supply chain spillover from {source_name} "
                            f"(score={source_sent:+.2f}, decay={effective_decay:.2f}, rev_exp={rev_pct:.0f}%, lag={lead_lag_hours}h). "
                            f"{rationale}"
                        )

                    # Keep highest absolute conviction if duplicate target
                    prev_cand = best_spillovers.get(dedup_key)
                    if prev_cand is None or abs(spill_score) > abs(prev_cand["spill_score"]):
                        best_spillovers[dedup_key] = {
                            "spill_score": spill_score,
                            "tuple": (
                                article_id,
                                target_asset_name,
                                self._infer_category(impact, target_asset_name),
                                None,  # sub_category
                                impact.get("country"),
                                impact.get("region"),
                                spill_score,
                                spill_confidence,
                                direction,
                                magnitude,
                                reasoning,
                                target_ticker,
                                None,  # policy_signal
                                True,  # is_spillover
                                article_id,  # spillover_source_article_id
                                source_name,  # spillover_source_asset
                                activation_date,  # activation_date
                            )
                        }

                    # =========================================================
                    # 6. Two-Hop Multi-Tier BFS Traversal (Cascading to Tier 3)
                    # =========================================================
                    if abs(source_sent) >= 0.65 and target_type == "ticker":
                        hop2_edges = self.graph.get_spillover_edges(target_key)
                        for hop2 in hop2_edges:
                            hop2_target = hop2.target_key
                            hop2_dedup = hop2_target.upper()

                            if hop2_dedup in direct_keys or hop2_dedup in first_hop_keys:
                                continue

                            # Upstream component / equipment multiplier
                            compounded_decay = effective_decay * hop2.decay_factor * 0.75
                            compounded_polarity = polarity * hop2.polarity
                            hop2_lag_hours = lead_lag_hours + hop2.lead_lag_hours
                            hop2_score = round(source_sent * compounded_decay * compounded_polarity, 4)

                            if abs(hop2_score) < NOISE_FLOOR:
                                continue

                            hop2_activation = published_ts + timedelta(hours=hop2_lag_hours)
                            hop2_conf = round(min(0.90, source_conf * 0.70), 3)
                            hop2_dir = "bullish" if hop2_score > 0.05 else ("bearish" if hop2_score < -0.05 else "neutral")
                            hop2_mag = "HIGH" if abs(hop2_score) >= 0.40 else ("MEDIUM" if abs(hop2_score) >= 0.18 else "LOW")
                            hop2_ticker = hop2_target if hop2.target_type == "ticker" else None

                            hop2_reasoning = (
                                f"[TIER-3 CASCADE] 2-hop spillover from {source_name} via {target_key} "
                                f"({hop2.relationship_type}, score={hop2_score:+.2f}, total_lag={hop2_lag_hours}h). "
                                f"{hop2.economic_rationale}"
                            )

                            prev_h2 = best_spillovers.get(hop2_dedup)
                            if prev_h2 is None or abs(hop2_score) > abs(prev_h2["spill_score"]):
                                best_spillovers[hop2_dedup] = {
                                    "spill_score": hop2_score,
                                    "tuple": (
                                        article_id,
                                        hop2_target,
                                        "EQUITY",
                                        None,
                                        impact.get("country"),
                                        impact.get("region"),
                                        hop2_score,
                                        hop2_conf,
                                        hop2_dir,
                                        hop2_mag,
                                        hop2_reasoning,
                                        hop2_ticker,
                                        None,
                                        True,
                                        article_id,
                                        source_name,
                                        hop2_activation
                                    )
                                }

        spillovers = [item["tuple"] for item in best_spillovers.values()]
        logger.debug(
            f"[SPILLOVER_ENGINE] Article {article_id}: {len(direct_impacts)} direct -> {len(spillovers)} verified spillover impacts."
        )
        return spillovers

    def _direct_impact_keys(self, impacts: List[Dict]) -> set:
        """Build set of uppercase asset names and tickers already directly tagged."""
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
        """Extract valid relationship graph lookup keys from an impact dictionary."""
        keys = []
        ticker = impact.get("ticker", "")
        asset_name = impact.get("asset_name", "")
        category = impact.get("asset_category", "")
        sub_cat = impact.get("sub_category", "")
        country = impact.get("country", "")

        if ticker:
            keys.append(ticker.strip().upper())

        if asset_name:
            keys.append(asset_name.strip())

        if category in ("SECTOR", "EQUITY"):
            sector_name = sub_cat or asset_name
            try:
                from ..routers.sentiment import SECTOR_NORM_MAP
                sector_canon = SECTOR_NORM_MAP.get(sector_name.upper(), sector_name)
            except Exception:
                sector_canon = sector_name

            if country:
                keys.append(f"{country}.{sector_canon}")
            keys.append(f"US.{sector_canon}")

        return keys

    def _resolve_cap(self, impact: Dict) -> float:
        cat = impact.get("asset_category", "")
        sub = impact.get("sub_category", "")
        if cat in ("SECTOR", "EQUITY", "CORPORATE"):
            return TYPE_DECAY_CAP["supply_chain"]
        elif cat == "ECONOMY":
            return TYPE_DECAY_CAP["macro_to_asset"]
        elif cat == "POLICY" and sub == "CENTRAL_BANK":
            return TYPE_DECAY_CAP["central_bank_to_asset"]
        elif cat == "RISK":
            return TYPE_DECAY_CAP["thematic_to_asset"]
        return TYPE_DECAY_CAP["default"]

    def _infer_category(self, impact: Dict, target_name: str) -> str:
        return "EQUITY"
