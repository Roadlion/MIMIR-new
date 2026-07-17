# scripts/test_sentiment.py
from app.sentiment.deepseek_client import DeepSeekSentiment

client = DeepSeekSentiment()
result = client.score_article(
    "BOJ Hikes Rates, Yen Surges",
    "The Bank of Japan raised interest rates by 25bps, signaling further tightening. USD/JPY drops 2%."
)
print(result)