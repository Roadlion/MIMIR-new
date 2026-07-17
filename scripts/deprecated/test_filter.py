# scripts/test_filter.py
import sys
import os
import re

# Set python path to backend directory
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "backend"))
from app.sentiment.deepseek_client import DeepSeekSentiment

client = DeepSeekSentiment()

test_cases = [
    # Positive Cases (Should be Kept)
    ("BOJ Hikes Rates, Yen Surges", "The Bank of Japan raised rates.", True),
    ("Nvidia stock hits record high after earnings beat", "Shares of Nvidia rose 5%.", True),
    ("US inflation falls to 3% in June", "CPI data shows cooling inflation.", True),
    ("OPEC+ agrees to oil production cut", "Crude oil prices expected to rise.", True),
    ("Global shipping rates double amid Red Sea crisis", "Supply chain disruptions continue.", True),
    ("Spotify stock jumps as album sales boost revenue", "Taylor Swift's album helped earnings.", True),
    ("Spirit Airlines files for bankruptcy protection", "The carrier cited debt and losses.", True),
    ("SpaceX valuation hits $150 billion in secondary share sale", "Private share sale details.", True),
    ("US to impose new tariffs on Chinese goods", "The trade war is escalating.", True),
    ("Federal court blocks JetBlue-Spirit merger", "The merger was blocked on antitrust concerns.", True),
    
    # Negative Cases (Should be Filtered)
    ("Taylor Swift wins Album of the Year at the Grammys", "The singer made history.", False),
    ("Local police arrest suspect in downtown bank robbery", "No one was injured.", False),
    ("US athlete wins gold medal in 100m sprint", "Olympic results from Paris.", False),
    ("New trailer released for Stranger Things Season 5", "Netflix shared a preview.", False),
    ("FC Barcelona wins Champions League match", "Sports update.", False),
    ("Scientists discover new species of deep ocean fish", "The creature was found at 4,000 meters.", False),
    ("Healthy salad recipes for summer dinner", "How to make a delicious and low calorie salad.", False),
]

print("🧪 Running Filter Relevance Verification Tests...\n")
passed = 0
for idx, (title, summary, expected) in enumerate(test_cases, 1):
    result = client.is_financial_or_macro(title, summary)
    if result == expected:
        print(f"✅ Pass {idx:02d}: '{title[:50]}' -> expected {expected}, got {result}")
        passed += 1
    else:
        print(f"❌ Fail {idx:02d}: '{title[:50]}' -> expected {expected}, got {result}")

print(f"\nResult: {passed}/{len(test_cases)} passed.")
if passed == len(test_cases):
    print("🎉 ALL TESTS PASSED SUCCESSFULLY!")
else:
    print("⚠️ SOME TESTS FAILED. CHECK LOGIC.")
    sys.exit(1)
