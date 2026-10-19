# backend/app/sentiment/pre_scoring_filter.py
"""
MIMIR Pre-Scoring Token Defense & Noise Gate
=============================================
Zero-LLM cost pre-filters that intercept and eliminate:
1. Sponsored posts, clickbait listicles, retail promotional fluff, and non-financial noise.
2. Exact and semantic duplicate syndicated headlines (Reuters/AP/Yahoo re-publications).

Saves 60%–80% of unnecessary LLM API tokens before any call is made to DeepSeek.
"""

import re
import hashlib
import logging
from typing import Dict, List, Tuple, Optional, Set, Any
from datetime import datetime, timezone, timedelta

from ..database import get_db_connection
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Common English and financial stopwords ignored during token set comparison
STOPWORDS: Set[str] = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but",
    "by", "could", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him",
    "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me",
    "more", "most", "my", "myself", "no", "nor", "not", "of", "off", "on", "once", "only", "or",
    "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves", "then",
    "there", "these", "they", "this", "those", "through", "to", "too", "under", "until", "up",
    "very", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom", "why",
    "with", "would", "you", "your", "yours", "yourself", "yourselves",
    # Common news noise words
    "breaking", "update", "exclusive", "alert", "report", "says", "said", "new", "today"
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. NOISE & CLICKBAIT FILTER RULES
# ─────────────────────────────────────────────────────────────────────────────

# Explicit phrases that immediately flag an article as promotional / junk
JUNK_PHRASES: List[str] = [
    "sponsored content", "partner content", "promoted by", "advertisement",
    "paid post", "presented by", "brought to you by", "advertorial",
    "motley fool", "zacks rank", "investorplace", "24/7 wall st",
    "could make you a millionaire", "if you invested $1,000", "if you invested $1000",
    "is it too late to buy", "before it explodes", "before they explode", "the next nvidia",
    "lottery ticket stock", "stocks to buy right now", "penny stocks to buy",
    "best stocks under $", "top stocks to buy now", "stocks to hold forever",
    "3 reasons to buy", "3 reasons to sell", "forget nvidia, buy",
    "forget tesla, buy", "forget apple, buy"
]

# Compiled regular expressions for listicles, clickbait, and lifestyle noise
CLICKBAIT_REGEXES: List[re.Pattern] = [
    # Listicles: "3 Best Growth Stocks", "5 Undervalued Dividend Stocks", "7 Stocks to Avoid"
    re.compile(r'\b\d+\s+(?:(?:best|top|hot|cheap|high-yield|undervalued|growth|dividend|tech|ai|penny)\s+)+(?:stocks|picks|buys)\b', re.IGNORECASE),
    re.compile(r'\b(?:top|best)\s+\d+\s+(?:stocks|picks|buys|dividend payers)\b', re.IGNORECASE),
    re.compile(r'\b\d+\s+(?:stocks|picks)\s+to\s+(?:buy|sell|avoid|hold)\b', re.IGNORECASE),
    re.compile(r'\b(?:stocks|equities)\s+to\s+(?:buy|sell|avoid|hold)\s+(?:now|right now|today|this week|in 202\d|forever)\b', re.IGNORECASE),
    re.compile(r'\b(?:buy\s+and\s+)?hold\s+forever\b', re.IGNORECASE),
    re.compile(r'\b\d+\s+reasons\s+(?:why\s+)?(?:to|you should)\s+(?:buy|sell|avoid)\b', re.IGNORECASE),
    re.compile(r'\b(?:can|will)\s+[\w\s]+\s+reach\s+\$?\d+[\d,]*\??\b', re.IGNORECASE),
    re.compile(r'\bwall\s+street\s+(?:analysts?\s+)?predicts?\s+(?:a\s+)?\d+%\s+upside\b', re.IGNORECASE),
    re.compile(r'\bbefore\s+(?:it|they)\s+explodes?\b', re.IGNORECASE),
]

LIFESTYLE_NOISE_REGEXES: List[re.Pattern] = [
    # Horoscopes / Astrology
    re.compile(r'\b(?:horoscope|astrology|zodiac|tarot|psychic reading)\b', re.IGNORECASE),
    # Recipes & Cooking
    re.compile(r'\b(?:recipe|recipes|baking|air fryer|weight loss diet|diet pill)\b', re.IGNORECASE),
    # Celebrity Gossip & Pop Culture
    re.compile(r'\b(?:celebrity gossip|red carpet|kardashian|dating rumors|wedding dress|divorce drama)\b', re.IGNORECASE),
    # Lottery / Gambling Promotions
    re.compile(r'\b(?:powerball|mega millions|lottery ticket|jackpot winning numbers)\b', re.IGNORECASE),
    # Movie / TV Shows
    re.compile(r'\b(?:box office trailer|movie review|netflix show|album tracklist)\b', re.IGNORECASE),
    # Sports Results (pure match scores)
    re.compile(r'\b(?:match highlights|final scoreline|penalty shootout|touchdown pass|atp tour draw)\b', re.IGNORECASE),
]


def is_noise_or_junk(title: str, summary: str = "") -> Tuple[bool, str]:
    """
    Evaluates whether an article is noise, clickbait, sponsored, or non-financial fluff.
    Returns:
        (is_noise: bool, reason: str)
    """
    if not title or len(title.strip()) < 12:
        return True, "TOO_SHORT_TITLE"

    words = title.strip().split()
    if len(words) < 3:
        return True, "TOO_FEW_WORDS"

    combined = f"{title} {summary or ''}".lower()

    # 1. Check explicit junk/promotional phrases
    for phrase in JUNK_PHRASES:
        if phrase in combined:
            return True, f"JUNK_PHRASE:{phrase}"

    # 2. Check clickbait & retail listicle patterns
    for regex in CLICKBAIT_REGEXES:
        if regex.search(title):
            return True, f"CLICKBAIT_REGEX:{regex.pattern[:30]}"

    # 3. Check non-financial / lifestyle patterns
    for regex in LIFESTYLE_NOISE_REGEXES:
        if regex.search(combined):
            return True, f"LIFESTYLE_NOISE:{regex.pattern[:30]}"

    return False, "CLEAN"


# ─────────────────────────────────────────────────────────────────────────────
# 2. HEADLINE NORMALIZATION & DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────

def normalize_headline(title: str) -> str:
    """
    Normalizes a headline for semantic comparison:
    - Strips source prefixes ('[Reuters] ', 'UPDATE 1-', 'EXCLUSIVE-')
    - Strips source attributions (' - Bloomberg', ' | CNBC')
    - Removes punctuation and standardizes spacing
    """
    if not title:
        return ""

    t = title.lower().strip()

    # Strip leading wire prefixes
    t = re.sub(
        r'^(?:\[.*?\]|\b(?:update\s*\d*|exclusive|flash|alert|breaking|watch|live|video|podcast|analysis|opinion|column|report)\s*[-:]\s*)',
        '', t, flags=re.IGNORECASE
    )

    # Strip trailing source attributions
    t = re.sub(
        r'\s*[-|–—]\s*(?:bloomberg|reuters|cnbc|wsj|the wall street journal|marketwatch|yahoo finance|investing\.com|seeking alpha|benzinga|barron\'s|forbes|ft|financial times|associated press|ap).*$',
        '', t, flags=re.IGNORECASE
    )

    # Remove currency symbols and punctuation
    t = re.sub(r'[\$€£¥]', '', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def get_normalized_title_hash(title: str) -> str:
    """Returns MD5 hash of the normalized headline."""
    norm = normalize_headline(title)
    return hashlib.md5(norm.encode('utf-8')).hexdigest()


def extract_informative_tokens(normalized_title: str) -> Set[str]:
    """Extracts non-stopword tokens from normalized headline with basic morphological stemming."""
    tokens = set(normalized_title.split())
    cleaned = set()
    for w in tokens:
        if len(w) <= 1 or w in STOPWORDS:
            continue
        stem = w
        if stem.endswith("ing") and len(stem) > 5:
            stem = stem[:-3]
        elif stem.endswith("ed") and len(stem) > 4:
            stem = stem[:-2]
        elif stem.endswith("es") and len(stem) > 4:
            stem = stem[:-2]
        elif stem.endswith("s") and len(stem) > 3:
            stem = stem[:-1]
        cleaned.add(stem)
    return cleaned


class HeadlineDeduplicator:
    """
    Maintains an in-memory sliding window cache of recent headlines
    from the database (last 48 hours) to detect:
    1. Exact normalized title matches.
    2. High-overlap syndicated wire rewrites (Jaccard similarity >= 0.65).
    """

    def __init__(self, lookback_hours: int = 48):
        self.lookback_hours = lookback_hours
        self._exact_hashes: Dict[str, int] = {}             # norm_hash -> article_id
        self._token_sets: List[Tuple[int, Set[str], str]] = [] # list of (article_id, token_set, norm_title)
        self._initialized = False

    def refresh_from_db(self, conn=None):
        """Loads scored and pending headlines from the last 48 hours into memory."""
        close_conn = False
        if conn is None:
            conn = get_db_connection()
            close_conn = True

        cur = conn.cursor()
        try:
            sql = f"""
                SELECT id, title 
                FROM {settings.mimir_schema}.mimir_raw_articles
                WHERE scraped_at >= NOW() - INTERVAL '{self.lookback_hours} hours'
                  AND scoring_status IN ('scored', 'pending', 'triage_pending')
                ORDER BY id DESC
                LIMIT 3000
            """
            cur.execute(sql)
            rows = cur.fetchall()

            self._exact_hashes.clear()
            self._token_sets.clear()

            for aid, title in rows:
                if not title:
                    continue
                norm = normalize_headline(title)
                h = hashlib.md5(norm.encode('utf-8')).hexdigest()
                self._exact_hashes[h] = aid

                tokens = extract_informative_tokens(norm)
                if len(tokens) >= 3:
                    self._token_sets.append((aid, tokens, norm))

            self._initialized = True
            logger.info(f"[DEDUPE] Loaded {len(self._exact_hashes)} recent headlines into memory deduplicator.")
        except Exception as e:
            logger.warning(f"[DEDUPE] Failed to warm deduplicator cache from DB: {e}")
            self._initialized = True
        finally:
            cur.close()
            if close_conn:
                conn.close()

    def is_duplicate(self, title: str, article_id: Optional[int] = None) -> Tuple[bool, Optional[int], str]:
        """
        Checks whether a title is an exact or semantic duplicate of an already seen article.
        Returns:
            (is_dup: bool, matched_article_id: Optional[int], match_reason: str)
        """
        if not self._initialized:
            self.refresh_from_db()

        norm = normalize_headline(title)
        if not norm:
            return False, None, ""

        norm_hash = hashlib.md5(norm.encode('utf-8')).hexdigest()

        # 1. Exact normalized hash match
        if norm_hash in self._exact_hashes:
            matched_id = self._exact_hashes[norm_hash]
            if article_id is None or matched_id != article_id:
                return True, matched_id, "EXACT_NORMALIZED_MATCH"

        # 2. Token Jaccard similarity for syndicated wire rewrites
        incoming_tokens = extract_informative_tokens(norm)
        if len(incoming_tokens) >= 4:
            for existing_id, existing_tokens, existing_norm in self._token_sets:
                if article_id is not None and existing_id == article_id:
                    continue

                intersection = len(incoming_tokens & existing_tokens)
                union = len(incoming_tokens | existing_tokens)
                if union == 0:
                    continue

                jaccard = intersection / union
                # If >= 65% of informative words match, it's the exact same wire story
                if jaccard >= 0.65:
                    return True, existing_id, f"JACCARD_SIMILARITY_{jaccard:.2f}"

        return False, None, ""

    def register(self, title: str, article_id: int):
        """Adds a newly processed article to the in-memory cache to catch duplicates in the same batch."""
        if not self._initialized:
            self.refresh_from_db()

        norm = normalize_headline(title)
        if not norm:
            return

        norm_hash = hashlib.md5(norm.encode('utf-8')).hexdigest()
        self._exact_hashes[norm_hash] = article_id

        tokens = extract_informative_tokens(norm)
        if len(tokens) >= 3:
            self._token_sets.append((article_id, tokens, norm))


# Singleton instance
_deduplicator = HeadlineDeduplicator()


def get_headline_deduplicator() -> HeadlineDeduplicator:
    global _deduplicator
    return _deduplicator


# ─────────────────────────────────────────────────────────────────────────────
# 3. BATCH FILTERING CONVENIENCE WRAPPER
# ─────────────────────────────────────────────────────────────────────────────

def filter_and_deduplicate_batch(
    articles: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Tuple[int, str]], List[Tuple[int, str]]]:
    """
    Takes a list of article dicts (must have 'id', 'title', and optionally 'summary').
    Filters them in-place with zero LLM API calls.
    Returns:
        (clean_articles, duplicate_items[(aid, reason)], noise_items[(aid, reason)])
    """
    deduper = get_headline_deduplicator()
    clean_articles = []
    duplicate_items = []
    noise_items = []

    for art in articles:
        aid = art["id"]
        title = art.get("title", "")
        summary = art.get("summary", "")

        # 1. Noise / Clickbait check
        is_junk, junk_reason = is_noise_or_junk(title, summary)
        if is_junk:
            noise_items.append((aid, junk_reason))
            continue

        # 2. Deduplication check
        is_dup, matched_id, dup_reason = deduper.is_duplicate(title, article_id=aid)
        if is_dup:
            duplicate_items.append((aid, f"{dup_reason}_MATCHES_{matched_id}"))
            continue

        # Passed all filters! Register in cache so future items in this batch don't duplicate it
        deduper.register(title, aid)
        clean_articles.append(art)

    return clean_articles, duplicate_items, noise_items
