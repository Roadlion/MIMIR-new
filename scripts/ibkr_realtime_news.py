import asyncio
from ib_async import IB, NewsTick

async def main():
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 7496, clientId=1)
    except Exception as e:
        print(f"Failed to connect. Error: {e}")

    # Define a callback for ANY macro or stock news hitting your TWS global feed
    def onGlobalNews(news: NewsTick):
        print(f"\n[GLOBAL NEWS FEED]")
        print(f"Provider: {news.providerCode}")
        print(f"Article ID: {news.articleId}")
        print(f"Headline: {news.headline}")
        
        # You can inspect the headline to screen for macro keywords (e.g., "FED", "CPI", "GDP")
        # Or automatically pull the body text using your prior logic:
        # asyncio.create_task(fetch_article_text(news.providerCode, news.articleId))

    # Bind the callback to the global news event handler
    ib.tickNewsEvent += onGlobalNews

    print("Listening for macro & market-wide news headlines...")
    while True:
        await asyncio.sleep(1)

asyncio.run(main())