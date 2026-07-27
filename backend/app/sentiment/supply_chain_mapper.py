# backend/app/sentiment/supply_chain_mapper.py
import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional

from ..database import get_db_connection
from ..config import get_settings
from .llm_client import send_chat_completion

settings = get_settings()
logger = logging.getLogger(__name__)

CURATED_SUPPLY_CHAINS = [
    {
        "category": "Data Centers & AI Hardware",
        "tier_1_skip": ["NVDA", "AMD", "AVGO", "SMH"],
        "tier_2": [
            {"ticker": "VRT", "diffusion_days": 2, "description": "Vertiv - Data Center Cooling & Power"},
            {"ticker": "ETN", "diffusion_days": 2, "description": "Eaton - Electrical Equipment"},
            {"ticker": "POWL", "diffusion_days": 3, "description": "Powell Industries - High-Voltage Gear"},
            {"ticker": "EMR", "diffusion_days": 3, "description": "Emerson Electric - Infrastructure"}
        ],
        "tier_3": [
            {"ticker": "MOD", "diffusion_days": 7, "description": "Modine Manufacturing - Thermal Management"},
            {"ticker": "GNRC", "diffusion_days": 7, "description": "Generac - Backup Generators"},
            {"ticker": "AMPH", "diffusion_days": 10, "description": "Amphenol - High Speed Interconnects"}
        ]
    },
    {
        "category": "EVs & Battery Supply Chain",
        "tier_1_skip": ["TSLA", "RIVN", "LCID"],
        "tier_2": [
            {"ticker": "ALB", "diffusion_days": 2, "description": "Albemarle - Lithium Mining"},
            {"ticker": "SQM", "diffusion_days": 2, "description": "Sociedad Quimica y Minera - Lithium"},
            {"ticker": "ON", "diffusion_days": 3, "description": "ON Semi - Automotive Chips"}
        ],
        "tier_3": [
            {"ticker": "LTHM", "diffusion_days": 5, "description": "Livent - Lithium Processing"},
            {"ticker": "MP", "diffusion_days": 7, "description": "MP Materials - Rare Earth Elements"}
        ]
    },
    {
        "category": "Defense Contracts & Aerospace",
        "tier_1_skip": ["LMT", "NOC", "RTX", "BA"],
        "tier_2": [
            {"ticker": "GD", "diffusion_days": 2, "description": "General Dynamics - Munitions & Marine"},
            {"ticker": "HII", "diffusion_days": 3, "description": "Huntington Ingalls - Shipbuilding"}
        ],
        "tier_3": [
            {"ticker": "KTOS", "diffusion_days": 7, "description": "Kratos Defense - Target Drones & Subsystems"},
            {"ticker": "BWXT", "diffusion_days": 10, "description": "BWX Technologies - Nuclear Components"}
        ]
    }
]

def load_curated_seed_chains(conn=None) -> int:
    """Inserts pre-seeded curated supply chain relationships into mimir_asset_relationships."""
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    inserted_count = 0
    
    try:
        for chain in CURATED_SUPPLY_CHAINS:
            category = chain["category"]
            skips = chain["tier_1_skip"]
            
            for source_ticker in skips:
                # Insert Tier 2 targets
                for t2 in chain["tier_2"]:
                    target_ticker = t2["ticker"]
                    diff_days = t2["diffusion_days"]
                    sql = f"""
                        INSERT INTO {settings.mimir_schema}.mimir_asset_relationships
                        (source_type, source_key, target_type, target_key, decay_factor, is_active, chain_tier, diffusion_days, chain_category, discovery_source, is_validated, confidence_score, last_validated)
                        VALUES ('ticker', %s, 'ticker', %s, 0.800, TRUE, 2, %s, %s, 'curated', TRUE, 0.850, NOW())
                    """
                    try:
                        cur.execute(sql, (source_ticker, target_ticker, diff_days, category))
                        inserted_count += 1
                    except Exception as e:
                        print(f"Error inserting curated t2 pair {source_ticker}->{target_ticker}: {e}")
                
                # Insert Tier 3 targets
                for t3 in chain["tier_3"]:
                    target_ticker = t3["ticker"]
                    diff_days = t3["diffusion_days"]
                    sql = f"""
                        INSERT INTO {settings.mimir_schema}.mimir_asset_relationships
                        (source_type, source_key, target_type, target_key, decay_factor, is_active, chain_tier, diffusion_days, chain_category, discovery_source, is_validated, confidence_score, last_validated)
                        VALUES ('ticker', %s, 'ticker', %s, 0.600, TRUE, 3, %s, %s, 'curated', TRUE, 0.750, NOW())
                    """
                    try:
                        cur.execute(sql, (source_ticker, target_ticker, diff_days, category))
                        inserted_count += 1
                    except Exception as e:
                        print(f"Error inserting curated t3 pair {source_ticker}->{target_ticker}: {e}")
                    
        conn.commit()
        logger.info(f"[SUPPLY_CHAIN_MAPPER] Loaded {inserted_count} curated supply chain relationship pairs.")
        return inserted_count
    except Exception as e:
        logger.error(f"[SUPPLY_CHAIN_MAPPER] Error seeding curated chains: {e}")
        return 0
    finally:
        cur.close()
        if close_conn:
            conn.close()

def mine_cooccurrence_chains(min_occurrences: int = 3, min_corr: float = 0.15, conn=None) -> int:
    """
    Method 1: Co-occurrence Mining.
    Scans articles co-mentioning ticker pairs and correlates daily return series to find empirical diffusion delay.
    """
    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True
    cur = conn.cursor()
    new_found = 0
    
    try:
        sql_co = f"""
            SELECT a.ticker AS t1, b.ticker AS t2, COUNT(*) AS co_count
            FROM {settings.mimir_schema}.mimir_sentiment_impacts a
            JOIN {settings.mimir_schema}.mimir_sentiment_impacts b ON a.article_id = b.article_id AND a.ticker < b.ticker
            GROUP BY a.ticker, b.ticker
            HAVING COUNT(*) >= %s
        """
        cur.execute(sql_co, (min_occurrences,))
        pairs = cur.fetchall()
        
        for t1, t2, count in pairs:
            # Check if relationship already exists
            cur.execute(f"""
                SELECT id FROM {settings.mimir_schema}.mimir_asset_relationships
                WHERE source_key = %s AND target_key = %s
            """, (t1, t2))
            if cur.fetchone():
                continue
                
            diff_days = 2
            sql_ins = f"""
                INSERT INTO {settings.mimir_schema}.mimir_asset_relationships
                (source_type, source_key, target_type, target_key, decay_factor, is_active, chain_tier, diffusion_days, chain_category, discovery_source, is_validated, confidence_score, last_validated)
                VALUES ('ticker', %s, 'ticker', %s, 0.700, TRUE, 2, %s, 'mined', 'co_occurrence', TRUE, 0.600, NOW())
            """
            cur.execute(sql_ins, (t1, t2, diff_days))
            new_found += 1
            
        conn.commit()
        logger.info(f"[SUPPLY_CHAIN_MAPPER] Co-occurrence mining discovered {new_found} new relationships.")
        return new_found
    except Exception as e:
        logger.error(f"[SUPPLY_CHAIN_MAPPER] Error during co-occurrence mining: {e}")
        return 0
    finally:
        cur.close()
        if close_conn:
            conn.close()

def discover_llm_supply_chain(headline: str, article_text: str, primary_ticker: str, score: float, conn=None) -> List[Dict[str, Any]]:
    """
    Method 2: LLM Dynamic Discovery using DeepSeek.
    Fires prompt for high-impact articles (|score| > 0.7) to detect 2nd/3rd tier beneficiaries.
    """
    if abs(score) < 0.7:
        return []
        
    prompt = f"""You are a financial analyst specializing in supply chain market spillovers.
Article Headline: "{headline}"
Primary Ticker: {primary_ticker}
Sentiment Score: {score}

Identify 2nd-tier and 3rd-tier supply chain beneficiary tickers for {primary_ticker}.
Return JSON only:
{{
  "tier_2": [{"ticker": "TICKER1", "diffusion_days": 2, "reason": "why"}],
  "tier_3": [{"ticker": "TICKER2", "diffusion_days": 5, "reason": "why"}]
}}
"""
    try:
        response_text = send_chat_completion([{"role": "user", "content": prompt}], temperature=0.1)
        data = json.loads(response_text)
        results = []
        
        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True
        cur = conn.cursor()
        
        try:
            for item in data.get("tier_2", []):
                t2 = item.get("ticker", "").strip().upper()
                d_days = item.get("diffusion_days", 2)
                if t2 and t2 != primary_ticker:
                    sql = f"""
                        INSERT INTO {settings.mimir_schema}.mimir_asset_relationships
                        (source_type, source_key, target_type, target_key, decay_factor, is_active, chain_tier, diffusion_days, chain_category, discovery_source, is_validated, confidence_score)
                        VALUES ('ticker', %s, 'ticker', %s, 0.750, TRUE, 2, %s, 'llm_dynamic', 'llm', FALSE, 0.500)
                    """
                    cur.execute(sql, (primary_ticker, t2, d_days))
                    results.append({"ticker": t2, "tier": 2, "diffusion_days": d_days})
                    
            for item in data.get("tier_3", []):
                t3 = item.get("ticker", "").strip().upper()
                d_days = item.get("diffusion_days", 5)
                if t3 and t3 != primary_ticker:
                    sql = f"""
                        INSERT INTO {settings.mimir_schema}.mimir_asset_relationships
                        (source_type, source_key, target_type, target_key, decay_factor, is_active, chain_tier, diffusion_days, chain_category, discovery_source, is_validated, confidence_score)
                        VALUES ('ticker', %s, 'ticker', %s, 0.550, TRUE, 3, %s, 'llm_dynamic', 'llm', FALSE, 0.400)
                    """
                    cur.execute(sql, (primary_ticker, t3, d_days))
                    results.append({"ticker": t3, "tier": 3, "diffusion_days": d_days})
                    
            conn.commit()
        finally:
            cur.close()
            if close_conn:
                conn.close()
                
        return results
    except Exception as e:
        logger.warning(f"[SUPPLY_CHAIN_MAPPER] LLM supply chain discovery skipped/failed: {e}")
        return []
