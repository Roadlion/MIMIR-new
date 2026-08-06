import pytest
import sys
from pathlib import Path

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.sentiment.deepseek_client import DeepSeekSentiment
from backend.app.pipeline.sentiment_processor import triage_pending_articles


def test_compressed_system_prompt():
    client = DeepSeekSentiment()
    prompt = client._get_system_prompt()
    batch_prompt = client._get_batch_system_prompt()
    
    assert len(prompt) < 2000, f"System prompt still too long: {len(prompt)} chars"
    assert len(batch_prompt) < 2000, f"Batch prompt still too long: {len(batch_prompt)} chars"
    assert "EQUITY" in prompt
    assert "TECHNOLOGY" in prompt
    assert "policy_signal" in prompt


def test_local_financial_or_macro_triage():
    client = DeepSeekSentiment()
    
    # Relevant financial headlines
    assert client.is_financial_or_macro("Nvidia Q3 earnings beat expectations, stock rallies", "Tech sector revenue surged 15%")
    assert client.is_financial_or_macro("Federal Reserve cuts interest rates by 25 bps", "Monetary policy easing announced by Fed Chair")
    assert client.is_financial_or_macro("OPEC+ cuts crude oil production targets", "Brent oil prices rise amid supply deficit")
    
    # Non-financial noise headlines
    assert not client.is_financial_or_macro("Manchester United wins football match 3-1", "Celebrity gossips at red carpet event")
    assert not client.is_financial_or_macro("Best recipes for baking chocolate cake", "Dog food nutrition guide and pet care tips")


def test_mock_batch_parsing(monkeypatch):
    client = DeepSeekSentiment()
    
    # Mock send_chat_completion to return a batch response
    def mock_send_chat_completion(messages, **kwargs):
        return '''{
          "results": [
            {
              "article_id": 1,
              "overall_sentiment": 0.5,
              "assets": [
                {
                  "asset_name": "Nvidia",
                  "asset_category": "EQUITY",
                  "sub_category": "TECHNOLOGY",
                  "country": "US",
                  "region": "NA",
                  "sentiment_score": 0.8,
                  "confidence": 0.95,
                  "direction": "bullish",
                  "magnitude": "HIGH",
                  "reasoning": "Strong AI chip demand",
                  "policy_signal": null
                }
              ]
            },
            {
              "article_id": 2,
              "overall_sentiment": -0.4,
              "assets": [
                {
                  "asset_name": "Crude Oil",
                  "asset_category": "COMMODITY",
                  "sub_category": "ENERGY",
                  "country": null,
                  "region": null,
                  "sentiment_score": -0.6,
                  "confidence": 0.85,
                  "direction": "bearish",
                  "magnitude": "MEDIUM",
                  "reasoning": "OPEC inventory build",
                  "policy_signal": null
                }
              ]
            }
          ]
        }'''
    
    import backend.app.sentiment.deepseek_client as dsc
    monkeypatch.setattr(dsc, "send_chat_completion", mock_send_chat_completion)
    
    test_articles = [
        {"id": 1, "title": "Nvidia reports record earnings", "summary": "AI chip maker beats Q3 revenue guidance", "force_relevance": True},
        {"id": 2, "title": "Crude oil plunges as inventories rise", "summary": "Energy markets react to surprise inventory surplus", "force_relevance": True}
    ]
    
    results = client.score_articles_batch(test_articles)
    assert 1 in results
    assert 2 in results
    assert results[1]["assets"][0]["asset_name"].lower() == "nvidia"
    assert results[1]["assets"][0]["ticker"] == "NVDA"
    assert results[2]["assets"][0]["asset_name"].lower() == "crude oil"


def test_mock_social_chatter(monkeypatch):
    client = DeepSeekSentiment()
    
    def mock_send_chat_completion(messages, **kwargs):
        return '''{
          "sentiment_score": 0.65,
          "confidence": 0.85,
          "direction": "bullish",
          "magnitude": "MEDIUM",
          "reasoning": "Subreddit chatter bullish on NVDA earnings."
        }'''
        
    import backend.app.sentiment.deepseek_client as dsc
    monkeypatch.setattr(dsc, "send_chat_completion", mock_send_chat_completion)
    
    res = client.score_social_chatter("NVDA", "Nvidia", "WallStreetBets discussion on NVDA options ahead of earnings")
    assert res["overall_sentiment"] == 0.65
    assert len(res["assets"]) == 1
    assert res["assets"][0]["ticker"] == "NVDA"
    assert res["assets"][0]["direction"] == "bullish"
