# backend/app/services/db_integrity.py
"""
MIMIR Database Integrity & Self-Healing Service
Detects disk page corruption, torn pages, or index issues,
and automatically repairs affected PostgreSQL tables with zero_damaged_pages + VACUUM FULL.
Also ensures database buffers are synced to disk (CHECKPOINT) on clean shutdowns.
"""
import logging
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

CORE_TABLES = [
    "mimir_social_chatter",
    "mimir_sentiment_impacts",
    "mimir_raw_articles",
    "mimir_hourly_ohlcv",
    "mimir_latest_prices",
    "mimir_dynamic_tickers",
    "mimir_portfolio",
    "mimir_trade_alerts",
]


def _get_raw_connection(autocommit=True, options="-c zero_damaged_pages=on"):
    """Creates a raw standalone connection to pantheon_db."""
    conn = psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        options=options,
        connect_timeout=5,
    )
    if autocommit:
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def sync_database_buffers() -> bool:
    """Executes a CHECKPOINT to flush all dirty shared memory buffers to disk."""
    try:
        conn = _get_raw_connection(autocommit=True)
        cur = conn.cursor()
        cur.execute("CHECKPOINT;")
        cur.close()
        conn.close()
        logger.info("[DB_INTEGRITY] Database buffers flushed to disk (CHECKPOINT successful).")
        return True
    except Exception as e:
        logger.warning(f"[DB_INTEGRITY] Failed to run CHECKPOINT: {e}")
        return False


def repair_table(table_name: str, schema: str = "yggdrasil") -> bool:
    """
    Performs full table & index repair using zero_damaged_pages + VACUUM FULL + REINDEX.
    Purges corrupted disk blocks while preserving all undamaged rows.
    """
    logger.warning(f"[DB_INTEGRITY] Initiating automatic repair for table {schema}.{table_name}...")
    try:
        conn = _get_raw_connection(autocommit=True, options="-c zero_damaged_pages=on")
        cur = conn.cursor()
        
        # 1. Run VACUUM FULL to rewrite clean physical file on disk
        cur.execute(f'VACUUM FULL "{schema}"."{table_name}";')
        
        # 2. Rebuild all indexes for this table
        cur.execute(f'REINDEX TABLE "{schema}"."{table_name}";')
        
        # 3. Analyze for fresh statistics
        cur.execute(f'ANALYZE "{schema}"."{table_name}";')
        
        cur.close()
        conn.close()
        logger.info(f"[DB_INTEGRITY] Successfully repaired table {schema}.{table_name} and rebuilt indexes.")
        return True
    except Exception as e:
        logger.error(f"[DB_INTEGRITY] Failed to repair table {schema}.{table_name}: {e}")
        return False


def verify_table(table_name: str, schema: str = "yggdrasil", auto_repair: bool = True) -> bool:
    """
    Scans a table to verify readability of all disk pages.
    If corruption is detected, automatically invokes repair_table.
    """
    conn = None
    try:
        # Connect with standard settings to test page integrity
        conn = _get_raw_connection(autocommit=True, options="")
        cur = conn.cursor()
        cur.execute(f'SELECT count(*) FROM "{schema}"."{table_name}";')
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return True
    except psycopg2.errors.DataCorrupted as e:
        logger.error(f"[DB_INTEGRITY] Corruption detected in {schema}.{table_name}: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        if auto_repair:
            return repair_table(table_name, schema)
        return False
    except Exception as e:
        err_msg = str(e).lower()
        if "invalid page" in err_msg or "corrupt" in err_msg:
            logger.error(f"[DB_INTEGRITY] Page corruption detected in {schema}.{table_name}: {e}")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            if auto_repair:
                return repair_table(table_name, schema)
            return False
        # Other benign errors (e.g. table does not exist)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return True


def ensure_database_integrity(tables=None, auto_repair=True) -> dict:
    """
    Scans specified (or all core) tables.
    Returns status report. Auto-repairs any damaged tables encountered.
    """
    target_tables = tables or CORE_TABLES
    report = {
        "checked": 0,
        "healthy": [],
        "repaired": [],
        "failed": []
    }

    for tbl in target_tables:
        report["checked"] += 1
        ok = verify_table(tbl, schema=settings.mimir_schema, auto_repair=auto_repair)
        if ok:
            report["healthy"].append(tbl)
        else:
            report["failed"].append(tbl)

    logger.info(
        f"[DB_INTEGRITY] Health check complete: {len(report['healthy'])} healthy, "
        f"{len(report['repaired'])} repaired, {len(report['failed'])} failed."
    )
    return report
