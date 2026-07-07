import asyncio
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from ib_async import IB, Stock, NewsTick
import hashlib
from datetime import datetime, timezone

HOST = "127.0.0.1"
PORT = 7496
CLIENT_ID = 1
REQUEST_TIMEOUT_SECONDS = 60

START_DATE = "2021-01-01"  # <-- add: earliest date you want
END_DATE = ""  # <-- add: "" = up to now
MAX_RESULTS_PER_CALL = 300  # <-- add: IBKR's per-call page cap

QUALIFY_BATCH_SIZE = 50
REQUEST_DELAY_SECONDS = 3.0
OUTPUT_CSV = "sp500_news_headlines.csv"
FETCH_ARTICLE_TEXT = True
IB_DATETIME_FMT = "%Y%m%d-%H:%M:%S"

CSV_COLUMNS = [
    "source_name",
    "feed_url",
    "title",
    "link",
    "published_raw",
    "published_ts",
    "summary",
    "url_hash",
    "scraped_at",
    "scoring_status",
    "title_hash",
]

def get_sp500_tickers():
    EXCLUDED_TICKERS = {"BK", "SATS", "CTRA", "HOLX"}
    url = "https://yfiua.github.io/index-constituents/constituents-sp500.json"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    return [item["Symbol"] for item in data if item["Symbol"] not in EXCLUDED_TICKERS]


def to_ibkr_symbol(ticker: str):
    return ticker.replace(".", " ")


def onTickNews(news: NewsTick):
    print(f"\n--- NEW HEADLINE [{news.providerCode}] ---")
    print(f"Time: {news.timeStamp}")
    print(f"Headline: {news.headline}")
    print(f"Article ID: {news.articleId}")


def provider_code(provider):
    return getattr(provider, "code", None) or getattr(provider, "providerCode", "")


def fmt_ib_datetime(dt: datetime) -> str:
    return dt.strftime(IB_DATETIME_FMT)


def parse_headline_time(headline_time):
    if isinstance(headline_time, datetime):
        if headline_time.tzinfo is None:
            return headline_time.replace(tzinfo=timezone.utc)  # <-- add this
        return headline_time
    for fmt in (IB_DATETIME_FMT, "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(headline_time), fmt).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
    return None


async def wait_for_request(awaitable):
    return await asyncio.wait_for(awaitable, timeout=REQUEST_TIMEOUT_SECONDS)


async def qualify_sp500_contracts(ib: IB):
    tickers = get_sp500_tickers()
    contracts = [
        (ticker, Stock(to_ibkr_symbol(ticker), "SMART", "USD")) for ticker in tickers
    ]
    qualified = []

    for start in range(0, len(contracts), QUALIFY_BATCH_SIZE):
        batch = contracts[start : start + QUALIFY_BATCH_SIZE]
        batch_contracts = [contract for _, contract in batch]
        try:
            result = await wait_for_request(ib.qualifyContractsAsync(*batch_contracts))
            qualified.extend(zip((ticker for ticker, _ in batch), result))
        except asyncio.TimeoutError:
            batch_symbols = ", ".join(ticker for ticker, _ in batch)
            print(f"Qualification timed out for batch: {batch_symbols}")
        except Exception as exc:
            batch_symbols = ", ".join(ticker for ticker, _ in batch)
            print(f"Qualification failed for batch {batch_symbols}: {exc}")

        await asyncio.sleep(1.0)

    print(f"Qualified {len(qualified)} / {len(contracts)} S&P 500 contracts")
    return qualified


async def fetch_headlines_for_contract(
    ib: IB,
    ticker: str,
    contract,
    provider_codes: str,
    target_start: datetime,
    initial_end_dt_str: str,
):
    all_rows = []
    seen_article_ids = set()
    end_dt_str = initial_end_dt_str
    oldest_seen = None

    while True:
        headlines = await wait_for_request(
            ib.reqHistoricalNewsAsync(
                conId=contract.conId,
                providerCodes=provider_codes,
                startDateTime=fmt_ib_datetime(target_start),
                endDateTime=end_dt_str,
                totalResults=MAX_RESULTS_PER_CALL,
            )
        )

        if not headlines:
            break

        page_times = []
        for headline in headlines:
            if headline.articleId in seen_article_ids:
                continue
            seen_article_ids.add(headline.articleId)

            h_time = parse_headline_time(headline.time)
            page_times.append(h_time)

            link = article_link(headline.providerCode, headline.articleId)
            title = headline.headline or ""
            published_raw = str(headline.time) if headline.time else ""
            scraped_at = datetime.now(timezone.utc).isoformat()

            row = {
                "id": None,
                "source_name": f"IBKR:{headline.providerCode}",
                "feed_url": "ibkr://historical-news",
                "title": title,
                "link": link,
                "published_raw": published_raw,
                "published_ts": published_raw,
                "summary": "",
                "url_hash": md5_text(link),
                "scraped_at": scraped_at,
                "scoring_status": "",
                "title_hash": md5_text(title.lower().strip()),
            }

            if FETCH_ARTICLE_TEXT:
                article = await wait_for_request(
                    ib.reqNewsArticleAsync(headline.providerCode, headline.articleId)
                )
                row["articleText"] = article.articleText

            all_rows.append(row)

        valid_times = [t for t in page_times if t is not None]
        if not valid_times:
            break

        new_oldest = min(valid_times)
        if oldest_seen is not None and new_oldest >= oldest_seen:
            break
        oldest_seen = new_oldest

        if new_oldest <= target_start:
            break
        if len(headlines) < MAX_RESULTS_PER_CALL:
            break

        end_dt_str = fmt_ib_datetime(new_oldest - timedelta(seconds=1))
        await asyncio.sleep(REQUEST_DELAY_SECONDS)

    return all_rows

def md5_text(value: str):
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def article_link(provider_code: str, article_id: str):
    return f"ibkr://news/{provider_code}/{article_id}"


async def main():
    ib = IB()
    await ib.connectAsync(HOST, PORT, clientId=CLIENT_ID)

    # <-- add these lines, right after connecting
    target_start = datetime.strptime(START_DATE, "%Y-%m-%d").replace(
        tzinfo=timezone.utc
    )
    if END_DATE:
        target_end_dt = datetime.strptime(END_DATE, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
        initial_end_dt_str = fmt_ib_datetime(target_end_dt)
    else:
        initial_end_dt_str = ""

    try:
        providers = await wait_for_request(ib.reqNewsProvidersAsync())
        provider_codes = "+".join(
            code for code in (provider_code(p) for p in providers) if code
        )
        if not provider_codes:
            raise RuntimeError(
                "No IBKR API news providers found for this account. "
                "Enable API news subscriptions in Account Management first."
            )
        print(f"Searching providers: {provider_codes}")

        contracts = await qualify_sp500_contracts(ib)
        all_rows = []

        print("\nFetching S&P 500 historical headlines...")
        for index, (ticker, contract) in enumerate(contracts, start=1):
            try:
                rows = await fetch_headlines_for_contract(
                    ib,
                    ticker,
                    contract,
                    provider_codes,
                    target_start,
                    initial_end_dt_str,  # <-- was just (ib, ticker, contract, provider_codes)
                )
                all_rows.extend(rows)
                print(f"[{index}/{len(contracts)}] {ticker}: {len(rows)} headlines")
            except asyncio.TimeoutError:
                print(f"[{index}/{len(contracts)}] {ticker}: timed out")
            except Exception as exc:
                print(f"[{index}/{len(contracts)}] {ticker}: failed - {exc}")

            await asyncio.sleep(REQUEST_DELAY_SECONDS)

        if all_rows:
            df = pd.DataFrame(all_rows, columns=CSV_COLUMNS)
            df.to_csv(OUTPUT_CSV, index=False)
            print(f"\nSaved {len(df)} headlines to {OUTPUT_CSV}")
        else:
            print("\nNo historical news found for S&P 500 contracts.")

    finally:
        ib.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
