import asyncio
from ib_async import *


async def connect_to_ibkr(ib):
    print("Connecting to IBKR...")
    await ib.connectAsync("127.0.0.1", 7497, clientId=1)
    print("Connected successfully!\n")


async def search_and_get_contract(ib):
    # Use asyncio.to_thread so blocking console input doesn't freeze the async engine
    query = await asyncio.to_thread(
        input, "Search for a stock ticker or company name (e.g., Apple, AMD, NVDA): "
    )
    query = query.strip()
    if not query:
        print("No search term entered.")
        return None

    print(f"Searching IBKR for '{query}'...")
    matches = await ib.reqMatchingSymbolsAsync(query)

    if not matches:
        print("No matching items found on Interactive Brokers.")
        return None

    # Extract only stock (STK) contracts to filter out options/futures options
    stock_matches = [m.contract for m in matches if m.contract.secType == "STK"]
    if not stock_matches:
        print("No stock contracts found matching that query.")
        return None

    # Present search results cleanly
    print("\n--- Match Results ---")
    for idx, contract in enumerate(stock_matches[:5]):  # show top 5 results
        print(
            f"[{idx}] Ticker: {contract.symbol} | Primary Exchange: {contract.primaryExchange} | Currency: {contract.currency}"
        )

    choice = await asyncio.to_thread(
        input, "\nSelect the index number you want to pull data for (Default 0): "
    )
    try:
        idx = int(choice.strip()) if choice.strip() else 0
        if idx < 0 or idx >= len(stock_matches):
            idx = 0
    except ValueError:
        idx = 0

    selected = stock_matches[idx]

    # Fully qualify the selected contract to populate critical back-end trade data
    print(f"\nQualifying selected contract details for {selected.symbol}...")
    qualified = await ib.qualifyContractsAsync(selected)
    return qualified[0] if qualified else None


async def historical_data_request(ib, contract):
    print(f"Requesting 30 days of historical daily charts for {contract.symbol}...")

    # Requesting past 30 days of 1-day timeframe candlestick bars
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr="30 D",  # Duration of historical log (e.g., '1 Y', '30 D', '1 W')
        barSizeSetting="1 day",  # Candlestick bar resolution ('1 min', '5 mins', '1 day')
        whatToShow="TRADES",  # Focus on historical transactions
        useRTH=True,  # Keep data restricted to Regular Trading Hours
    )
    return bars


async def main():
    ib = IB()

    try:
        await connect_to_ibkr(ib)

        # 1. Search dynamically for an asset
        contract = await search_and_get_contract(ib)

        if contract:
            # 2. Fetch its historical pricing arrays
            bars = await historical_data_request(ib, contract)

            if bars:
                print(f"\nSuccessfully retrieved {len(bars)} historical days.")

                # Option A: Quick console dump of the latest 5 rows
                print("\n--- Latest 5 Trading Rows ---")
                for bar in bars[-5:]:
                    print(
                        f"Date: {bar.date} | Open: {bar.open} | High: {bar.high} | Low: {bar.low} | Close: {bar.close} | Vol: {bar.volume}"
                    )

                # Option B: If you have pandas installed, uncomment these lines for an elegant data grid
                # print("\n--- Dataframe View ---")
                # df = util.df(bars)
                # print(df.tail(10).to_string(index=False))
            else:
                print("No data structures returned from query parameters.")

    except Exception as e:
        print(f"An error occurred: {e}")

    finally:
        if ib.isConnected():
            ib.disconnect()
            print("\nDisconnected from IBKR.")


if __name__ == "__main__":
    asyncio.run(main())
