# scripts/test_deepseek.py

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from backend.app.sentiment.deepseek_client import DeepSeekSentiment

client = DeepSeekSentiment()

# Test with asset scoring
result = client.score_article_with_assets(
    "Oil Prices Surge as OPEC+ Cuts Output, Dollar Weakens",
    "OPEC+ announced surprise production cuts of 1.6 million barrels per day. Oil jumps 5% while the dollar index falls to a two-week low. Investors flock to safe-haven assets."
)

print(json.dumps(result, indent=2))