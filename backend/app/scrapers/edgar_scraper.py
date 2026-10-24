# backend/app/scrapers/edgar_scraper.py
"""
MIMIR SEC EDGAR 8-K Real-Time Scraper
======================================
Polls the SEC EDGAR Atom RSS feed for newly filed 8-K reports every 10 minutes.
Routes high-signal item types directly into MIMIR's article pipeline as
`source_type = "SEC_8K"` with `force_relevance = True`.

Why 8-Ks?
  Retail investors don't read SEC filings. Institutions cover large-caps already.
  The edge is small/mid-cap 8-Ks that sit unnoticed for hours before price moves.

High-signal 8-K Item Numbers:
  1.01 — Material Definitive Agreement (contract wins, partnerships)
  1.03 — Bankruptcy/Receivership
  2.02 — Results of Operations (flash earnings)
  5.02 — Departure/Appointment of CEO/CFO
  7.01 — Regulation FD Disclosure (guidance)
  8.01 — Other Events (FDA decisions, government contracts, patent grants)
"""

import re
import time
import hashlib
import logging
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# SEC EDGAR full-index: latest 40 8-K filings (Atom feed, no auth required)
EDGAR_8K_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&dateb=&owner=include"
    "&count=40&search_text=&output=atom"
)

# High-signal item numbers — these produce trade-relevant catalysts
HIGH_SIGNAL_ITEMS = {
    "1.01": "Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "2.02": "Results of Operations (Earnings)",
    "5.02": "Executive Departure / Appointment",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Material Event (FDA/Contract/Patent)",
}

MEDIUM_SIGNAL_ITEMS = {
    "1.02": "Termination of Material Agreement",
    "2.01": "Completion of Acquisition",
    "3.01": "Deregistration of Securities",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "5.01": "Changes in Control of Registrant",
    "9.01": "Financial Statements and Exhibits",
}

# Request headers to be polite to SEC servers
EDGAR_HEADERS = {
    "User-Agent": "MIMIR Research Tool research@mimir.local",  # SEC requires identification
    "Accept-Encoding": "gzip, deflate",
    "Host": "www.sec.gov",
}

# Atom namespace
_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Track seen filing accession numbers to avoid reprocessing
_seen_accessions: set = set()
_seen_accessions_max = 500  # prevent unbounded growth


def _extract_item_numbers(title: str, summary: str) -> List[str]:
    """Extract 8-K item numbers from title or summary text."""
    combined = f"{title} {summary}"
    # Match patterns like "Item 1.01", "Items 1.01, 8.01", "1.01 Material..."
    found = re.findall(r'\b(\d+\.\d+)\b', combined)
    return list(set(found))


def _score_item_priority(item_numbers: List[str]) -> str:
    """Returns 'HIGH', 'MEDIUM', or 'LOW' based on item numbers found."""
    for item in item_numbers:
        if item in HIGH_SIGNAL_ITEMS:
            return "HIGH"
    for item in item_numbers:
        if item in MEDIUM_SIGNAL_ITEMS:
            return "MEDIUM"
    return "LOW"


def _parse_company_from_title(title: str) -> str:
    """
    SEC EDGAR titles look like:
      "8-K - ACME Corp (0001234567) (Filer)"
    Extract the company name.
    """
    # Strip "8-K - " prefix
    cleaned = re.sub(r'^8-K\s*[-–]\s*', '', title).strip()
    # Remove CIK in parens
    cleaned = re.sub(r'\(\d{10}\)', '', cleaned).strip()
    # Remove trailing "(Filer)" or similar
    cleaned = re.sub(r'\(Filer\)$', '', cleaned).strip()
    return cleaned


def _map_company_to_ticker(company_name: str) -> Optional[str]:
    """
    Attempt to map a company name to a known ticker via MIMIR's asset_mapper.
    Returns None if not found.
    """
    try:
        from ..sentiment.asset_mapper import resolve_ticker
        ticker, found = resolve_ticker(company_name)
        return ticker if found else None
    except Exception:
        return None


def fetch_latest_8k_filings(max_age_hours: int = 2) -> List[Dict]:
    """
    Fetches the latest 8-K filings from SEC EDGAR Atom feed.
    Filters to:
      - Filed within the last max_age_hours
      - Not already seen (deduped by accession number)
      - HIGH or MEDIUM priority items only

    Returns a list of article-like dicts ready for the MIMIR pipeline.
    """
    try:
        resp = requests.get(EDGAR_8K_FEED, headers=EDGAR_HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"[EDGAR] Failed to fetch 8-K feed: {e}")
        return []

    results = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        logger.error(f"[EDGAR] XML parse error: {e}")
        return []

    entries = root.findall("atom:entry", _NS)
    logger.info(f"[EDGAR] Feed returned {len(entries)} entries.")

    for entry in entries:
        try:
            # Extract fields
            title_el = entry.find("atom:title", _NS)
            link_el = entry.find("atom:link", _NS)
            summary_el = entry.find("atom:summary", _NS)
            updated_el = entry.find("atom:updated", _NS)
            id_el = entry.find("atom:id", _NS)

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            summary = summary_el.text.strip() if summary_el is not None and summary_el.text else ""
            updated_str = updated_el.text.strip() if updated_el is not None and updated_el.text else ""
            accession_id = id_el.text.strip() if id_el is not None and id_el.text else link

            # Parse timestamp
            try:
                filed_at = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            except Exception:
                filed_at = datetime.now(timezone.utc)

            # Age filter
            if filed_at < cutoff:
                continue

            # Dedup
            accession_hash = hashlib.md5(accession_id.encode()).hexdigest()
            if accession_hash in _seen_accessions:
                continue

            # Extract item numbers and score
            item_numbers = _extract_item_numbers(title, summary)
            priority = _score_item_priority(item_numbers)
            if priority == "LOW":
                continue  # Skip routine 8-Ks (exhibits, minor items)

            # Extract company name
            company_name = _parse_company_from_title(title)
            ticker = _map_company_to_ticker(company_name)

            # Build item type description for the article summary
            item_descriptions = []
            for item in item_numbers:
                desc = HIGH_SIGNAL_ITEMS.get(item) or MEDIUM_SIGNAL_ITEMS.get(item)
                if desc:
                    item_descriptions.append(f"Item {item}: {desc}")

            item_summary = " | ".join(item_descriptions) if item_descriptions else "SEC 8-K Filing"

            # Build article-like dict for MIMIR pipeline
            enhanced_title = f"[SEC 8-K] {company_name}: {item_summary}"
            enhanced_summary = (
                f"SEC 8-K Filing — {company_name}. {item_summary}. "
                f"Filed: {filed_at.strftime('%Y-%m-%d %H:%M UTC')}. "
                f"Priority: {priority}. "
                + (f"Original summary: {summary[:400]}" if summary else "")
            )

            # Build title hash for dedup in mimir_raw_articles
            title_hash = hashlib.md5(enhanced_title.lower().encode()).hexdigest()
            url_hash = hashlib.md5(link.encode()).hexdigest()

            results.append({
                "title": enhanced_title,
                "summary": enhanced_summary,
                "link": link,
                "published_raw": updated_str,
                "published_ts": filed_at,
                "source_name": "SEC_8K",
                "source_type": "SEC_8K",
                "feed_url": EDGAR_8K_FEED,
                "title_hash": title_hash,
                "url_hash": url_hash,
                "force_relevance": True,  # Skip MIMIR's keyword filter
                "scoring_status": "pending",
                "priority": priority,
                "company_name": company_name,
                "ticker_hint": ticker,  # Pre-resolved ticker hint for catalyst engine
                "item_numbers": item_numbers,
                "item_descriptions": item_descriptions,
            })

            # Mark as seen
            _seen_accessions.add(accession_hash)
            # Trim seen set if too large
            if len(_seen_accessions) > _seen_accessions_max:
                oldest = list(_seen_accessions)[:100]
                for h in oldest:
                    _seen_accessions.discard(h)

        except Exception as entry_err:
            logger.warning(f"[EDGAR] Error parsing entry: {entry_err}")
            continue

    logger.info(f"[EDGAR] {len(results)} new HIGH/MEDIUM priority 8-K filings found.")
    return results


def insert_edgar_articles(articles: List[Dict], conn=None) -> int:
    """
    Inserts 8-K articles into mimir_raw_articles for downstream
    DeepSeek scoring. Uses force_relevance=True to skip keyword triage.
    Returns number of new articles inserted.
    """
    if not articles:
        return 0

    from ..database import get_db_connection
    from ..config import get_settings

    settings = get_settings()
    schema = settings.mimir_schema

    close_conn = False
    if conn is None:
        conn = get_db_connection()
        close_conn = True

    cur = conn.cursor()
    inserted = 0

    try:
        for art in articles:
            try:
                cur.execute(f"""
                    INSERT INTO {schema}.mimir_raw_articles
                        (source_name, feed_url, title, link, published_raw, published_ts,
                         summary, url_hash, title_hash, scoring_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (title_hash) DO NOTHING
                    RETURNING id
                """, (
                    art["source_name"],
                    art["feed_url"],
                    art["title"],
                    art["link"],
                    art["published_raw"],
                    art["published_ts"],
                    art["summary"],
                    art["url_hash"],
                    art["title_hash"],
                    "pending",  # Ready for DeepSeek scoring immediately
                ))
                row = cur.fetchone()
                if row:
                    inserted += 1
                    logger.info(
                        f"[EDGAR] Inserted 8-K article id={row[0]}: "
                        f"{art['title'][:80]}"
                    )
            except Exception as art_err:
                logger.warning(f"[EDGAR] Insert error for '{art.get('title', '')[:60]}': {art_err}")
                conn.rollback()
                cur = conn.cursor()
                continue

        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"[EDGAR] Batch insert error: {e}")
    finally:
        cur.close()
        if close_conn:
            conn.close()

    return inserted


def run_edgar_scrape_cycle() -> int:
    """
    Full cycle: fetch latest 8-Ks → insert new ones → return count inserted.
    Called by the background worker every 10 minutes.
    """
    logger.info("[EDGAR] Starting 8-K scrape cycle...")
    articles = fetch_latest_8k_filings(max_age_hours=2)
    if not articles:
        logger.info("[EDGAR] No new 8-K filings to process.")
        return 0

    n = insert_edgar_articles(articles)
    logger.info(f"[EDGAR] Cycle complete. {n} new 8-K articles inserted into pipeline.")
    return n
