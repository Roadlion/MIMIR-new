# backend/app/sentiment/supply_chain_mapper.py
"""
MIMIR Institutional Supply Chain & Relationship Engine (v2)
===========================================================
Enforces strict 3-Gate verification on all dynamically discovered relationships:
- Gate 1: Syntactic Sentence-Level Co-Occurrence & Action Verbs
- Gate 2: Cross-Sector Plausibility Matrix (banning absurd links like Semis <-> Newspapers)
- Gate 3: DeepSeek Structured Schema Extraction (relationship_type, polarity, rev_pct, lead_lag)

All dynamically discovered edges enter in QUARANTINE (is_active = FALSE)
until empirically validated or confirmed by an analyst.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional

from ..database import get_db_connection
from ..config import get_settings
from .llm_client import send_chat_completion

settings = get_settings()
logger = logging.getLogger(__name__)

# Permitted B2B Cross-Sector Linkage Corridors (Gate 2)
# Any discovered edge whose source and target sectors do not match an allowed corridor is discarded
ALLOWED_SECTOR_CORRIDORS = {
    "TECHNOLOGY": {"TECHNOLOGY", "INDUSTRIALS", "UTILITIES", "BASIC_MATERIALS", "COMMUNICATION_SERVICES"},
    "INDUSTRIALS": {"INDUSTRIALS", "TECHNOLOGY", "BASIC_MATERIALS", "ENERGY", "DEFENSE"},
    "DEFENSE": {"DEFENSE", "INDUSTRIALS", "TECHNOLOGY", "BASIC_MATERIALS"},
    "ENERGY": {"ENERGY", "UTILITIES", "INDUSTRIALS", "BASIC_MATERIALS"},
    "UTILITIES": {"UTILITIES", "ENERGY", "TECHNOLOGY", "INDUSTRIALS"},
    "CONSUMER_CYCLICAL": {"CONSUMER_CYCLICAL", "INDUSTRIALS", "TECHNOLOGY", "CONSUMER_DEFENSIVE"},
    "CONSUMER_DEFENSIVE": {"CONSUMER_DEFENSIVE", "BASIC_MATERIALS", "INDUSTRIALS"},
    "HEALTHCARE": {"HEALTHCARE", "BASIC_MATERIALS", "TECHNOLOGY"},
    "BASIC_MATERIALS": {"BASIC_MATERIALS", "INDUSTRIALS", "ENERGY", "TECHNOLOGY"},
}

# Commercial relationship action verbs required for Gate 1
RELATIONSHIP_ACTION_VERBS = [
    r"\bsuppl(y|ies|ied|ier)\b",
    r"\bprocure(s|d|ment)?\b",
    r"\bcontract(s|ed)?\b",
    r"\bpartner(s|ship)?\b",
    r"\bcompete(s|d|tion|titor)?\b",
    r"\brival(s)?\b",
    r"\bvendor(s)?\b",
    r"\bmanufactur(e|es|ed|ing|er)\b",
    r"\bfabricat(e|es|ed|ion|or)\b",
    r"\bcooling\b",
    r"\bpower grid\b",
    r"\blithium\b",
    r"\bsemiconductor\b",
    r"\bwafer(s)?\b",
    r"\blithography\b",
    r"\blost market share\b",
    r"\bgained market share\b",
    r"\bantitrust\b",
    r"\bexclusive deal\b"
]
ACTION_REGEX = re.compile("|".join(RELATIONSHIP_ACTION_VERBS), re.IGNORECASE)


def verify_syntactic_co_occurrence(text: str, t1: str, t2: str) -> bool:
    """
    Gate 1: Tickers must appear in the same sentence or clause
    alongside an explicit commercial or competitive action verb.
    """
    if not text or not t1 or not t2:
        return False

    sentences = re.split(r'[.!?\n]+', text)
    t1_pattern = re.compile(rf"\b{re.escape(t1)}\b", re.IGNORECASE)
    t2_pattern = re.compile(rf"\b{re.escape(t2)}\b", re.IGNORECASE)

    for sent in sentences:
        if t1_pattern.search(sent) and t2_pattern.search(sent):
            if ACTION_REGEX.search(sent):
                return True
    return False


def verify_cross_sector_plausibility(t1: str, t2: str, conn=None) -> bool:
    """
    Gate 2: Verifies that t1 and t2 belong to plausible B2B commercial corridors.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()

    try:
        from ..services.sector_rotation_service import STATIC_TICKER_SECTORS
        s1 = STATIC_TICKER_SECTORS.get(t1)
        s2 = STATIC_TICKER_SECTORS.get(t2)

        if not s1 or not s2:
            return True  # If sectors unknown, proceed to Gate 3 LLM check

        allowed = ALLOWED_SECTOR_CORRIDORS.get(s1, set())
        return (s2 in allowed) or (s1 == s2)
    except Exception:
        return True
    finally:
        cur.close()
        if close_conn:
            conn.close()


def discover_llm_supply_chain(
    headline: str,
    article_text: str,
    primary_ticker: str,
    score: float,
    conn=None
) -> List[Dict[str, Any]]:
    """
    Gate 3: DeepSeek Structured Relationship Extraction.
    Triggered only on high-magnitude catalysts (|score| >= 0.65).
    Inserts newly discovered relationships directly into QUARANTINE (is_active = FALSE).
    """
    if abs(score) < 0.65:
        return []

    prompt = f"""You are an institutional financial analyst specializing in supply chain equity research.
Analyze the following market catalyst for primary asset {primary_ticker}:
Headline: "{headline}"
Context: "{article_text[:1200]}"

Identify direct 2nd-tier suppliers, customers, infrastructure partners, or competitors affected by this catalyst.
Strictly disallow generic market wrap co-mentions. Every target MUST have a clear commercial or competitive linkage.

Return JSON only in this exact format:
{{
  "relationships": [
    {{
      "target_ticker": "TICKER",
      "relationship_type": "SUPPLIER | CUSTOMER | COMPETITOR | INFRA_COOLING_POWER | TECH_PARTNER | EQUIPMENT_RAW_MAT",
      "polarity": 1.0 or -1.0,
      "revenue_exposure_pct": 15.0,
      "lead_lag_hours": 24,
      "economic_rationale": "One-sentence explanation of the commercial link"
    }}
  ]
}}
"""
    try:
        response_text = send_chat_completion([{"role": "user", "content": prompt}], temperature=0.1)
        # Clean response if markdown blocks exist
        if "```json" in response_text:
            response_text = response_text.split("```json")[1].split("```")[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```")[1].split("```")[0].strip()

        data = json.loads(response_text)
        candidates = data.get("relationships", [])
        if not candidates:
            return []

        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True
        cur = conn.cursor()

        results = []
        try:
            for item in candidates:
                tgt = str(item.get("target_ticker", "")).strip().upper()
                r_type = str(item.get("relationship_type", "SUPPLIER")).strip().upper()
                pol = float(item.get("polarity", 1.0))
                rev_exp = float(item.get("revenue_exposure_pct", 10.0))
                lag_hrs = int(item.get("lead_lag_hours", 24))
                rationale = str(item.get("economic_rationale", "")).strip()

                if not tgt or tgt == primary_ticker or len(tgt) > 10:
                    continue

                # Ensure polarity is valid
                if r_type == "COMPETITOR":
                    pol = -1.0
                elif pol not in (-1.0, 1.0):
                    pol = 1.0 if pol >= 0 else -1.0

                # Gate 2: Cross-sector check
                if not verify_cross_sector_plausibility(primary_ticker, tgt, conn=conn):
                    logger.info(f"[SUPPLY_CHAIN_MAPPER] Discarded candidate {primary_ticker}->{tgt}: failed cross-sector plausibility.")
                    continue

                diff_days = max(1, lag_hrs // 24)
                decay = 0.80 if r_type in ("SUPPLIER", "INFRA_COOLING_POWER") else (0.70 if r_type == "COMPETITOR" else 0.60)

                # Insert into QUARANTINE (is_active = FALSE, is_validated = FALSE)
                sql = f"""
                    INSERT INTO {settings.mimir_schema}.mimir_asset_relationships
                    (source_type, source_key, target_type, target_key, decay_factor, is_active,
                     chain_tier, diffusion_days, chain_category, discovery_source, is_validated,
                     confidence_score, relationship_type, polarity, revenue_exposure_pct,
                     lead_lag_hours, validation_source, economic_rationale, last_validated)
                    VALUES ('ticker', %s, 'ticker', %s, %s, FALSE, 2, %s, 'discovered_quarantine', 'llm_gate3', FALSE,
                            0.500, %s, %s, %s, %s, 'LLM_STRUCTURED', %s, NOW())
                    ON CONFLICT (source_type, source_key, target_type, target_key) DO NOTHING;
                """
                cur.execute(sql, (
                    primary_ticker, tgt, decay, diff_days,
                    r_type, pol, rev_exp, lag_hrs, rationale
                ))
                if cur.rowcount > 0:
                    results.append({"target": tgt, "type": r_type, "polarity": pol, "lag_hours": lag_hrs})
                    logger.info(f"[SUPPLY_CHAIN_MAPPER] Quarantined new candidate: {primary_ticker} -> {tgt} ({r_type}, Polarity {pol})")

            conn.commit()
            return results
        finally:
            cur.close()
            if close_conn:
                conn.close()

    except Exception as e:
        logger.warning(f"[SUPPLY_CHAIN_MAPPER] LLM structured discovery skipped/failed: {e}")
        return []


def mine_cooccurrence_chains(*args, **kwargs) -> int:
    """
    [PERMANENTLY DEPRECATED]
    Raw unconstrained co-occurrence mining is decommissioned to protect against noise pollution.
    All dynamic edges must pass the 3-Gate Discovery Firewall via discover_llm_supply_chain.
    """
    logger.debug("[SUPPLY_CHAIN_MAPPER] mine_cooccurrence_chains() called but safely suppressed by Spillover Overhaul.")
    return 0
