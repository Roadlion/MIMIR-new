# backend/app/analytics/options_data.py
import time
import threading
import yfinance as yf
import pandas as pd
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import List, Dict, Optional, Tuple

@dataclass
class OptionContract:
    strike: float
    bid: float
    ask: float
    mid: float
    last: float
    volume: int
    open_interest: int
    implied_volatility: float
    in_the_money: bool
    days_to_expiry: int
    contract_symbol: str
    
    @property
    def bid_ask_spread_pct(self) -> float:
        if self.mid <= 0:
            return 0.0
        return (self.ask - self.bid) / self.mid
    
    @property  
    def liquidity_grade(self) -> str:
        if self.volume > 1000 and self.open_interest > 5000 and self.bid_ask_spread_pct < 0.1:
            return 'HIGH'
        elif self.volume > 100 or self.open_interest > 500:
            return 'MEDIUM'
        return 'LOW'


@dataclass
class OptionsChain:
    ticker: str
    underlying_price: float
    expirations: List[date]
    calls: Dict[date, List[OptionContract]]
    puts: Dict[date, List[OptionContract]]
    fetched_at: datetime


class OptionsDataProvider(ABC):
    @abstractmethod
    def fetch_chain(self, ticker: str) -> OptionsChain:
        pass

    @abstractmethod
    def fetch_underlying_price(self, ticker: str) -> float:
        pass

    @abstractmethod
    def get_expirations(self, ticker: str) -> List[date]:
        pass


class YFinanceOptionsProvider(OptionsDataProvider):
    def fetch_underlying_price(self, ticker: str) -> float:
        try:
            print(f"[CASINO_OPTIONS] Fetching underlying price for {ticker} from yfinance")
            t = yf.Ticker(ticker)
            history = t.history(period="1d")
            if history.empty:
                info = t.info
                if 'currentPrice' in info:
                    return info['currentPrice']
                elif 'regularMarketPrice' in info:
                    return info['regularMarketPrice']
                else:
                    raise ValueError(f"No price data available for {ticker}")
            return float(history['Close'].iloc[-1])
        except Exception as e:
            print(f"[CASINO_OPTIONS] Error fetching underlying price for {ticker}: {e}")
            raise

    def get_expirations(self, ticker: str) -> List[date]:
        try:
            print(f"[CASINO_OPTIONS] Fetching expirations for {ticker} from yfinance")
            t = yf.Ticker(ticker)
            opts = t.options
            if not opts:
                print(f"[CASINO_OPTIONS] Warning: No options available for {ticker}")
                return []
            return [datetime.strptime(d, '%Y-%m-%d').date() for d in opts]
        except Exception as e:
            print(f"[CASINO_OPTIONS] Error fetching expirations for {ticker}: {e}")
            raise

    def _row_to_contract(self, row: dict, dte: int) -> OptionContract:
        bid = float(row.get('bid', 0.0) if not pd.isna(row.get('bid')) else 0.0)
        ask = float(row.get('ask', 0.0) if not pd.isna(row.get('ask')) else 0.0)
        mid = (bid + ask) / 2
        
        iv = row.get('impliedVolatility', 0.0)
        if pd.isna(iv) or iv is None or iv < 0.001:
            iv = 0.0
            
        return OptionContract(
            strike=float(row.get('strike', 0.0)),
            bid=bid,
            ask=ask,
            mid=mid,
            last=float(row.get('lastPrice', 0.0) if not pd.isna(row.get('lastPrice')) else 0.0),
            volume=int(row.get('volume', 0) if not pd.isna(row.get('volume')) else 0),
            open_interest=int(row.get('openInterest', 0) if not pd.isna(row.get('openInterest')) else 0),
            implied_volatility=float(iv),
            in_the_money=bool(row.get('inTheMoney', False)),
            days_to_expiry=dte,
            contract_symbol=str(row.get('contractSymbol', ''))
        )

    def fetch_chain(self, ticker: str) -> OptionsChain:
        try:
            print(f"[CASINO_OPTIONS] Fetching full options chain for {ticker} from yfinance")
            t = yf.Ticker(ticker)
            underlying = self.fetch_underlying_price(ticker)
            str_dates = t.options
            
            expirations = []
            calls = {}
            puts = {}
            
            today = date.today()
            
            for d in str_dates:
                exp_date = datetime.strptime(d, '%Y-%m-%d').date()
                expirations.append(exp_date)
                dte = (exp_date - today).days
                
                try:
                    chain = t.option_chain(d)
                except Exception as e:
                    print(f"[CASINO_OPTIONS] Warning: Could not fetch chain for {ticker} exp {d}: {e}")
                    calls[exp_date] = []
                    puts[exp_date] = []
                    continue
                    
                calls[exp_date] = [
                    self._row_to_contract(r.to_dict(), dte) 
                    for _, r in chain.calls.iterrows()
                ]
                puts[exp_date] = [
                    self._row_to_contract(r.to_dict(), dte) 
                    for _, r in chain.puts.iterrows()
                ]
                
            return OptionsChain(
                ticker=ticker,
                underlying_price=underlying,
                expirations=expirations,
                calls=calls,
                puts=puts,
                fetched_at=datetime.now()
            )
        except Exception as e:
            print(f"[CASINO_OPTIONS] Error fetching chain for {ticker}: {e}")
            raise


class SyntheticOptionsProvider(OptionsDataProvider):
    DEFAULT_PRICES: Dict[str, float] = {
        'AAPL': 225.0, 'MSFT': 440.0, 'NVDA': 120.0, 'TSLA': 240.0,
        'AMZN': 185.0, 'GOOGL': 175.0, 'META': 500.0, 'SPY': 550.0,
        'QQQ': 480.0, 'IWM': 220.0
    }

    def fetch_underlying_price(self, ticker: str) -> float:
        return self.DEFAULT_PRICES.get(ticker.upper(), 150.0)

    def get_expirations(self, ticker: str) -> List[date]:
        from datetime import timedelta
        today = date.today()
        return [
            today + timedelta(days=30),
            today + timedelta(days=60),
            today + timedelta(days=90)
        ]

    def fetch_chain(self, ticker: str) -> OptionsChain:
        from datetime import timedelta
        underlying = self.fetch_underlying_price(ticker)
        expirations = self.get_expirations(ticker)
        today = date.today()
        calls: Dict[date, List[OptionContract]] = {}
        puts: Dict[date, List[OptionContract]] = {}

        for exp in expirations:
            dte = (exp - today).days
            calls[exp] = []
            puts[exp] = []
            
            strikes = [round(underlying * f, 1) for f in [0.85, 0.90, 0.95, 1.0, 1.05, 1.10, 1.15]]
            for s in strikes:
                dist = abs(s - underlying) / underlying
                call_prem = max(0.5, (underlying - s) + underlying * 0.05 * (1 - dist))
                put_prem = max(0.5, (s - underlying) + underlying * 0.05 * (1 - dist))
                
                calls[exp].append(OptionContract(
                    strike=s, bid=round(call_prem * 0.95, 2), ask=round(call_prem * 1.05, 2),
                    mid=round(call_prem, 2), last=round(call_prem, 2), volume=1500,
                    open_interest=5000, implied_volatility=0.28, in_the_money=(s < underlying),
                    days_to_expiry=dte, contract_symbol=f"{ticker}{exp.strftime('%y%m%d')}C{int(s*1000)}"
                ))
                puts[exp].append(OptionContract(
                    strike=s, bid=round(put_prem * 0.95, 2), ask=round(put_prem * 1.05, 2),
                    mid=round(put_prem, 2), last=round(put_prem, 2), volume=1500,
                    open_interest=5000, implied_volatility=0.28, in_the_money=(s > underlying),
                    days_to_expiry=dte, contract_symbol=f"{ticker}{exp.strftime('%y%m%d')}P{int(s*1000)}"
                ))

        return OptionsChain(
            ticker=ticker,
            underlying_price=underlying,
            expirations=expirations,
            calls=calls,
            puts=puts,
            fetched_at=datetime.now()
        )


class OptionsDataService:
    def __init__(self, providers: List[OptionsDataProvider]):
        self.providers = providers
        self._cache_chain: Dict[str, Tuple[OptionsChain, float]] = {}
        self._lock = threading.Lock()
        self._ttl_seconds = 300  # 5 minutes

    def fetch_chain(self, ticker: str) -> OptionsChain:
        with self._lock:
            now = time.time()
            if ticker in self._cache_chain:
                chain, timestamp = self._cache_chain[ticker]
                if now - timestamp < self._ttl_seconds:
                    print(f"[CASINO_OPTIONS] Cache hit for chain {ticker}")
                    return chain
                    
        for provider in self.providers:
            try:
                chain = provider.fetch_chain(ticker)
                with self._lock:
                    self._cache_chain[ticker] = (chain, time.time())
                return chain
            except Exception as e:
                print(f"[CASINO_OPTIONS] Provider {provider.__class__.__name__} failed for chain {ticker}: {e}")
                
        raise Exception(f"All providers failed to fetch chain for {ticker}")

    def fetch_underlying_price(self, ticker: str) -> float:
        for provider in self.providers:
            try:
                return provider.fetch_underlying_price(ticker)
            except Exception as e:
                print(f"[CASINO_OPTIONS] Provider {provider.__class__.__name__} failed for underlying {ticker}: {e}")
        raise Exception(f"All providers failed to fetch underlying price for {ticker}")

    def get_expirations(self, ticker: str) -> List[date]:
        for provider in self.providers:
            try:
                return provider.get_expirations(ticker)
            except Exception as e:
                print(f"[CASINO_OPTIONS] Provider {provider.__class__.__name__} failed for expirations {ticker}: {e}")
        raise Exception(f"All providers failed to fetch expirations for {ticker}")


_options_service: Optional[OptionsDataService] = None

def get_options_service() -> OptionsDataService:
    global _options_service
    if _options_service is None:
        _options_service = OptionsDataService([YFinanceOptionsProvider(), SyntheticOptionsProvider()])
    return _options_service
