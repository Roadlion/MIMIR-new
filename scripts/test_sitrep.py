# scripts/test_sitrep.py
import sys
import os

if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.services.sitrep_service import compile_sitrep, fetch_macro_snapshot
from backend.app.analytics.narrative_tracker import update_active_narratives, get_all_active_narratives, get_multi_day_narrative_context

def test_sitrep_system():
    print("=== 1. Testing Macro Snapshot & Yield Low/High Detection ===")
    macro_snap = fetch_macro_snapshot()
    print(f"Fetched {len(macro_snap)} macro benchmarks.")
    for sym, data in macro_snap.items():
        print(f"  [{sym}] {data['name']}: {data['current']} (1D: {data['change_1d_pct']:+.2f}%, 5D: {data['change_5d_pct']:+.2f}%, Range: {data['range_pct']}%)")
        if data.get("is_at_low"):
            print(f"   🚨 AT 52-WEEK LOW: {sym}")
        if data.get("is_at_high"):
            print(f"   🚨 AT 52-WEEK HIGH: {sym}")

    print("\n=== 2. Testing Multi-Day Narrative Matrix Synthesis ===")
    active_nars = update_active_narratives()
    print(f"Synthesized {len(active_nars)} active multi-day narratives.")
    for nar in active_nars[:3]:
        print(f"  • [{nar['phase']}] {nar['theme']} (Avg Sent: {nar['avg_sentiment']:+.2f}, Count: {nar['article_count']})")

    context = get_multi_day_narrative_context("NVDA")
    print(f"\nNarrative Context for NVDA: {context}")

    print("\n=== 3. Testing Complete Sit Rep Compilation ===")
    sitrep = compile_sitrep(report_type="TEST_RUN")
    print(f"Sit Rep compiled successfully (ID: {sitrep.get('id')}).")
    print(f"Title: {sitrep.get('title')}")
    print(f"Headline Summary: {sitrep.get('headline_summary')}")
    print("\n--- Full Sit Rep Preview (First 500 chars) ---")
    print(sitrep.get('full_markdown', '')[:500])
    print("...")

if __name__ == "__main__":
    test_sitrep_system()
