# backend/app/sentiment/relationship_graph.py
"""
In-memory cache of asset relationships for spillover propagation.
Loaded from mimir_asset_relationships table at startup, refreshed periodically.
"""
import logging
from typing import List, Dict, Tuple, Optional
from ..database import get_db_connection

logger = logging.getLogger(__name__)

# ponytail: global singleton, fine unless multi-process — then per-worker refresh handles it
_graph_instance: Optional["RelationshipGraph"] = None


class RelationshipGraph:
    def __init__(self):
        self._edges: Dict[str, List[Tuple[str, str, float]]] = {}
        # source_key -> [(target_type, target_key, decay_factor), ...]
        self._loaded_at = None
        self._edge_count = 0

    def load_from_db(self) -> int:
        """Fetch all active relationships. Returns count loaded."""
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT source_type, source_key, target_type, target_key, decay_factor
                FROM yggdrasil.mimir_asset_relationships
                WHERE is_active = TRUE
                ORDER BY source_type, source_key
            """)
            rows = cur.fetchall()
            cur.close()

            edges: Dict[str, List[Tuple[str, str, float]]] = {}
            for source_type, source_key, target_type, target_key, decay in rows:
                edges.setdefault(source_key, []).append(
                    (target_type, target_key, float(decay))
                )

            self._edges = edges
            self._edge_count = len(rows)
            import datetime
            self._loaded_at = datetime.datetime.now()
            logger.info(
                "RelationshipGraph loaded: %d edges, %d source keys",
                self._edge_count, len(self._edges),
            )
            return self._edge_count
        except Exception as e:
            logger.warning("RelationshipGraph load failed: %s", e)
            return 0
        finally:
            if conn:
                conn.close()

    def get_spillover_targets(
        self, source_key: str
    ) -> List[Tuple[str, str, float]]:
        """Return (target_type, target_key, decay_factor) for a source key."""
        # Try exact match first, then case-insensitive
        if source_key in self._edges:
            return self._edges[source_key]
        lower = source_key.lower()
        for k, v in self._edges.items():
            if k.lower() == lower:
                return v
        return []

    def get_targets_by_type(
        self, source_key: str, target_type: str
    ) -> List[Tuple[str, str, float]]:
        """Filter spillover targets by target_type (e.g. 'ticker', 'asset_name')."""
        all_targets = self.get_spillover_targets(source_key)
        return [(tt, tk, d) for tt, tk, d in all_targets if tt == target_type]

    def get_tier2_targets(self, source_key: str) -> List[Tuple[str, str, float]]:
        """Return Tier 2 supply chain targets for a source ticker/key."""
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT target_type, target_key, decay_factor, diffusion_days
                FROM yggdrasil.mimir_asset_relationships
                WHERE source_key = %s AND chain_tier = 2 AND is_validated = TRUE AND is_active = TRUE
            """, (source_key.upper(),))
            rows = cur.fetchall()
            cur.close()
            return [(r[0], r[1], float(r[2])) for r in rows]
        except Exception:
            return []
        finally:
            if conn:
                conn.close()

    def get_tier3_targets(self, source_key: str) -> List[Tuple[str, str, float]]:
        """Return Tier 3 supply chain targets for a source ticker/key."""
        conn = None
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT target_type, target_key, decay_factor, diffusion_days
                FROM yggdrasil.mimir_asset_relationships
                WHERE source_key = %s AND chain_tier = 3 AND is_validated = TRUE AND is_active = TRUE
            """, (source_key.upper(),))
            rows = cur.fetchall()
            cur.close()
            return [(r[0], r[1], float(r[2])) for r in rows]
        except Exception:
            return []
        finally:
            if conn:
                conn.close()

    def get_skip_list(self, source_key: str) -> List[str]:
        """Return frontline tickers to skip for a given catalyst source."""
        # Tier 1 frontline tickers are skipped for new position entry
        return [source_key.upper()]

    def has_source(self, source_key: str) -> bool:
        return bool(self.get_spillover_targets(source_key))

    @property
    def edge_count(self) -> int:
        return self._edge_count

    @property
    def loaded_at(self):
        return self._loaded_at


def get_relationship_graph() -> RelationshipGraph:
    """Return the global singleton, loading if necessary."""
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = RelationshipGraph()
        _graph_instance.load_from_db()
    return _graph_instance


def refresh_relationship_graph() -> int:
    """Reload from DB. Returns edge count."""
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = RelationshipGraph()
    return _graph_instance.load_from_db()
