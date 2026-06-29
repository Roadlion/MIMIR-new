"""Test Guerilla Quant niche API endpoints end-to-end."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests

BASE = "http://127.0.0.1:8000/api/v1"


def test_opportunities():
    r = requests.get(f"{BASE}/niche/opportunities")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    data = r.json()
    assert "opportunities" in data
    print(f"[OK] /niche/opportunities -> {len(data['opportunities'])} opportunities")


def test_signals():
    r = requests.get(f"{BASE}/niche/signals?days=30&limit=10")
    assert r.status_code == 200
    data = r.json()
    assert "signals" in data
    print(f"[OK] /niche/signals -> {len(data['signals'])} signals")


def test_stats():
    r = requests.get(f"{BASE}/niche/stats")
    assert r.status_code == 200
    data = r.json()
    assert "active_pairs" in data
    assert "high_conviction_sigs" in data
    print(f"[OK] /niche/stats -> pairs={data['active_pairs']} high_conv={data['high_conviction_sigs']}")


def test_articles():
    r = requests.get(f"{BASE}/niche/articles?days=3&limit=5")
    assert r.status_code == 200
    data = r.json()
    assert "articles" in data
    print(f"[OK] /niche/articles -> {len(data['articles'])} articles")


def test_articles_with_ticker():
    r = requests.get(f"{BASE}/niche/articles?ticker1=CORN&days=7&limit=5")
    assert r.status_code == 200
    print(f"[OK] /niche/articles?ticker1=CORN -> {len(r.json()['articles'])} articles")


if __name__ == "__main__":
    try:
        print("Testing Guerilla Quant API endpoints...")
        test_opportunities()
        test_signals()
        test_stats()
        test_articles()
        test_articles_with_ticker()
        print("\nAll niche API tests passed.")
    except Exception as e:
        print(f"\n[FAIL] {e}")
        sys.exit(1)
