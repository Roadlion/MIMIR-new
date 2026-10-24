import os, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import MetaTrader5 as mt5
from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()

# 1. Get currently open tickers from MT5
open_tickers = []
if mt5.initialize():
    positions = mt5.positions_get()
    if positions:
        for p in positions:
            d = p._asdict()
            open_tickers.append((d['symbol'], d['profit']))
    mt5.shutdown()

print("=" * 75)
print("       WAR RIG CYLINDER CONTRIBUTIONS FOR CURRENT ACTIVE FLEET")
print("=" * 75)

conn = get_db_connection()
cur = conn.cursor()

for ticker, profit in open_tickers:
    cur.execute(f"""
        SELECT ticker, conviction_score, sentiment_score, headline, reason, investment_thesis, created_at
        FROM {settings.mimir_schema}.mimir_trade_signals
        WHERE ticker = %s AND catalyst_type = 'WAR_RIG_CONVERGENCE'
        ORDER BY created_at DESC LIMIT 1
    """, (ticker,))
    row = cur.fetchone()
    if not row:
        continue
    
    tk, conv, sent, hl, reason, thesis, dt = row
    dt_str = dt.strftime('%m-%d %H:%M') if dt else 'N/A'
    
    # Check sentiment impacts in database scored by DeepSeek
    cur.execute(f"""
        SELECT a.title, si.sentiment_score, si.confidence, si.magnitude, si.reasoning, a.source_name, a.published_ts
        FROM {settings.mimir_schema}.mimir_sentiment_impacts si
        JOIN {settings.mimir_schema}.mimir_raw_articles a ON si.article_id = a.id
        WHERE si.ticker = %s
        ORDER BY a.published_ts DESC LIMIT 3
    """, (ticker,))
    impacts = cur.fetchall()

    print(f"\n[{tk}] Floating PnL: ${profit:+,.2f} | Conviction: {float(conv)*100:.0f}% (Raw: {conv}) | Signal Sent: {sent:+.2f}")
    print(f"  Reason: {reason}")
    print(f"  Thesis: {thesis[:200] if thesis else 'N/A'}...")
    if impacts:
        print(f"  Top DeepSeek Scored Articles ({len(impacts)}):")
        for imp in impacts:
            t_art, s_score, conf, mag, r_text, src, pub = imp
            pub_str = pub.strftime('%m-%d %H:%M') if pub else 'N/A'
            print(f"    - [{src} | {pub_str}] Sent: {s_score:+.2f} ({mag}) | {t_art[:80]}...")
            if r_text:
                print(f"      Reasoning: {r_text[:120]}...")
    else:
        print("  Top DeepSeek Scored Articles: None directly logged on ticker.")

cur.close()
conn.close()
