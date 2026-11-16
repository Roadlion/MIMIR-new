# backend/app/sentiment/relationship_graph.py
"""
MIMIR Institutional Relationship Graph (v2)
===========================================
In-memory directed relationship graph supporting:
- Rich edge metadata: relationship_type, polarity, lead_lag_hours, revenue_exposure_pct
- Competitor vs Supplier polarity awareness (+1.0 vs -1.0)
- 30-minute TTL cache hot-reloading (automatically picks up newly seeded/validated edges)
- Multi-tier cascading lookups
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional, Any

from ..database import get_db_connection

logger = logging.getLogger(__name__)

# Global singleton instance
_graph_instance: Optional["RelationshipGraph"] = None


@dataclass
class RelationshipEdge:
    target_type: str
    target_key: str
    decay_factor: float
    polarity: float = 1.00
    relationship_type: str = "SUPPLIER"
    lead_lag_hours: int = 24
    revenue_exposure_pct: float = 10.00
    chain_tier: int = 2
    economic_rationale: str = ""

    # Backwards compatibility with tuple unpacking: (target_type, target_key, decay_factor)
    def __iter__(self):
        yield self.target_type
        yield self.target_key
        yield self.decay_factor
        yield self.polarity
        yield self.relationship_type
        yield self.lead_lag_hours
        yield self.revenue_exposure_pct

    def __getitem__(self, idx):
        items = (
            self.target_type, self.target_key, self.decay_factor,
            self.polarity, self.relationship_type, self.lead_lag_hours,
            self.revenue_exposure_pct, self.chain_tier, self.economic_rationale
        )
        return items[idx]


class RelationshipGraph:
    def __init__(self):
        self._edges: Dict[str, List[RelationshipEdge]] = {}
        # source_key -> [RelationshipEdge, ...]
        self._loaded_at: Optional[datetime] = None
        self._edge_count: int = 0

    def load_from_db(self) -> int:
        """Fetch all active, validated relationships from PostgreSQL."""
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT source_type, source_key, target_type, target_key, decay_factor,
                       COALESCE(polarity, 1.00),
                       COALESCE(relationship_type, 'SUPPLIER'),
                       COALESCE(lead_lag_hours, 24),
                       COALESCE(revenue_exposure_pct, 10.00),
                       COALESCE(chain_tier, 2),
                       COALESCE(economic_rationale, '')
                FROM yggdrasil.mimir_asset_relationships
                WHERE is_active = TRUE
                ORDER BY source_type, source_key
            """)
            rows = cur.fetchall()
            cur.close()

            edges: Dict[str, List[RelationshipEdge]] = {}
            for (
                source_type, source_key, target_type, target_key, decay,
                polarity, rel_type, lag_hrs, rev_pct, tier, rationale
            ) in rows:
                edge = RelationshipEdge(
                    target_type=target_type,
                    target_key=target_key,
                    decay_factor=float(decay),
                    polarity=float(polarity),
                    relationship_type=str(rel_type),
                    lead_lag_hours=int(lag_hrs),
                    revenue_exposure_pct=float(rev_pct),
                    chain_tier=int(tier),
                    economic_rationale=str(rationale)
                )
                edges.setdefault(source_key.upper(), []).append(edge)

            self._edges = edges
            self._edge_count = len(rows)
            self._loaded_at = datetime.now()
            logger.info(
                f"[RELATIONSHIP_GRAPH] Loaded {self._edge_count} active edges across {len(self._edges)} source assets."
            )
            return self._edge_count
        except Exception as e:
            logger.warning(f"[RELATIONSHIP_GRAPH] Load failed: {e}")
            return 0
        finally:
            if conn:
                conn.close()

    def get_spillover_edges(self, source_key: str) -> List[RelationshipEdge]:
        """Return rich RelationshipEdge objects for a source key."""
        if not source_key:
            return []
        key_upper = source_key.strip().upper()
        if key_upper in self._edges:
            return self._edges[key_upper]
        # Case-insensitive fallback
        for k, v in self._edges.items():
            if k.upper() == key_upper:
                return v
        return []

    def get_spillover_targets(self, source_key: str) -> List[RelationshipEdge]:
        """Alias returning rich RelationshipEdge objects (supports legacy tuple indexing)."""
        return self.get_spillover_edges(source_key)

    def get_targets_by_type(self, source_key: str, target_type: str) -> List[RelationshipEdge]:
        """Filter spillover targets by target_type (e.g. 'ticker', 'asset_name')."""
        all_targets = self.get_spillover_edges(source_key)
        return [e for e in all_targets if e.target_type == target_type]

    def get_skip_list(self, source_key: str) -> List[str]:
        """Return frontline tickers to skip for entry (Trade the ripple, not the splash)."""
        return [source_key.strip().upper()]

    def has_source(self, source_key: str) -> bool:
        return bool(self.get_spillover_edges(source_key))

    @property
    def edge_count(self) -> int:
        return self._edge_count

    @property
    def loaded_at(self) -> Optional[datetime]:
        return self._loaded_at


def get_relationship_graph() -> RelationshipGraph:
    """
    Return the global singleton instance.
    Automatically hot-reloads every 30 minutes if fresh relationships were seeded or validated.
    """
    global _graph_instance
    now = datetime.now()
    if _graph_instance is None or (_graph_instance.loaded_at and (now - _graph_instance.loaded_at) > timedelta(minutes=30)):
        _graph_instance = RelationshipGraph()
        _graph_instance.load_from_db()
    return _graph_instance


def refresh_relationship_graph() -> int:
    """Manually force an immediate reload from DB."""
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = RelationshipGraph()
    return _graph_instance.load_from_db()
