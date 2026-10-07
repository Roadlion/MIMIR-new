# backend/app/integration/mt5_executor.py
"""
Direct MetaTrader 5 Real Order Execution Bridge for MIMIR.
Sends real BUY/SELL market orders, stop-losses, take-profits, and position closures
directly to the connected MT5 broker terminal via the MetaTrader5 Python API.
"""

import sys
import time
from typing import Dict, Any, List, Optional
from datetime import datetime

try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    mt5 = None
    HAS_MT5 = False

DEFAULT_MAGIC_NUMBER = 202608
DEFAULT_LOT_SIZE = 0.01
LIVE_TRADING_ENABLED = True  # Master toggle for direct MT5 order execution


def initialize_mt5() -> bool:
    """Initializes connection to MT5 terminal if not already connected."""
    if not HAS_MT5:
        print("[MT5 EXECUTOR ERROR] MetaTrader5 Python library is not installed.")
        return False

    # Check if MT5 terminal connection is active
    terminal_info = mt5.terminal_info()
    if terminal_info is not None:
        return True

    if not mt5.initialize():
        print(f"[MT5 EXECUTOR ERROR] MT5 initialization failed: {mt5.last_error()}")
        return False

    print("[MT5 EXECUTOR SUCCESS] Connected to MetaTrader 5 Terminal.")
    return True


def resolve_broker_symbol(symbol: str) -> Optional[str]:
    """
    Resolves standard ticker symbol (e.g. AAPL) to broker-specific symbol format
    (e.g., AAPL.US, #AAPL, AAPL) in the MT5 Market Watch window.
    """
    if not initialize_mt5():
        return None

    clean_symbol = symbol.strip().upper()
    candidates = [
        clean_symbol,
        f"{clean_symbol}.US",
        f"#{clean_symbol}",
        clean_symbol.replace(".", ""),
        f"{clean_symbol.replace('.', '')}.US",
    ]
    if clean_symbol == "BTCUSD":
        candidates.extend(["BTCUSD", "BTC", "BTC/USD"])

    for cand in candidates:
        symbol_info = mt5.symbol_info(cand)
        if symbol_info is not None:
            if not symbol_info.visible:
                mt5.symbol_select(cand, True)
            return cand

    return None


def get_filling_mode(symbol: str) -> int:
    """
    Determines the correct order filling mode supported by the broker for the given symbol
    (ORDER_FILLING_IOC, ORDER_FILLING_FOK, or ORDER_FILLING_RETURN).
    """
    if not HAS_MT5:
        return 0

    info = mt5.symbol_info(symbol)
    if info is None:
        return mt5.ORDER_FILLING_IOC

    filling_flags = info.filling_mode

    # 1: FOK (Fill or Kill), 2: IOC (Immediate or Cancel), 4: RETURN (Return)
    if filling_flags & 2:  # SYMBOL_FILLING_IOC
        return mt5.ORDER_FILLING_IOC
    elif filling_flags & 1:  # SYMBOL_FILLING_FOK
        return mt5.ORDER_FILLING_FOK
    else:
        return mt5.ORDER_FILLING_RETURN


def get_mt5_account_info() -> Dict[str, Any]:
    """Retrieves account details (balance, equity, margin, leverage, profit) from MT5."""
    if not initialize_mt5():
        return {"status": "OFFLINE", "error": "MT5 Terminal not connected."}

    info = mt5.account_info()
    if info is None:
        return {"status": "ERROR", "error": f"Failed to get account info: {mt5.last_error()}"}

    info_dict = info._asdict()
    return {
        "status": "ONLINE",
        "login": info_dict.get("login"),
        "server": info_dict.get("server"),
        "currency": info_dict.get("currency"),
        "balance": info_dict.get("balance"),
        "equity": info_dict.get("equity"),
        "margin": info_dict.get("margin"),
        "free_margin": info_dict.get("margin_free"),
        "margin_level": info_dict.get("margin_level"),
        "profit": info_dict.get("profit"),
        "leverage": info_dict.get("leverage"),
        "name": info_dict.get("name"),
    }


def get_mt5_positions(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieves list of active open positions directly from MT5 terminal."""
    if not initialize_mt5():
        return []

    if symbol:
        b_symbol = resolve_broker_symbol(symbol)
        positions = mt5.positions_get(symbol=b_symbol) if b_symbol else []
    else:
        positions = mt5.positions_get()

    if positions is None:
        return []

    results = []
    for pos in positions:
        p_dict = pos._asdict()
        results.append({
            "ticket": p_dict.get("ticket"),
            "symbol": p_dict.get("symbol"),
            "type": "BUY" if p_dict.get("type") == 0 else "SELL",
            "volume": p_dict.get("volume"),
            "price_open": p_dict.get("price_open"),
            "price_current": p_dict.get("price_current"),
            "sl": p_dict.get("sl"),
            "tp": p_dict.get("tp"),
            "profit": p_dict.get("profit"),
            "swap": p_dict.get("swap"),
            "magic": p_dict.get("magic"),
            "comment": p_dict.get("comment"),
            "time": datetime.fromtimestamp(p_dict.get("time")).strftime('%Y-%m-%d %H:%M:%S') if p_dict.get("time") else None,
        })
    return results


def send_mt5_order(
    ticker: str,
    action: str,  # 'OPEN_LONG', 'OPEN_SHORT', 'CLOSE_LONG', 'CLOSE_SHORT', 'BUY', 'SELL'
    volume: float = DEFAULT_LOT_SIZE,
    sl_pct: float = 0.0,
    tp_pct: float = 0.0,
    magic: int = DEFAULT_MAGIC_NUMBER,
    comment: str = "MIMIR AI Strategy",
) -> Dict[str, Any]:
    """
    Sends a direct market order to MT5 terminal. Handles position opening and position closing.
    """
    if not LIVE_TRADING_ENABLED:
        return {"success": False, "message": "Direct MT5 live trading is currently disabled."}

    if not initialize_mt5():
        return {"success": False, "message": "MT5 Terminal initialization failed."}

    broker_symbol = resolve_broker_symbol(ticker)
    if not broker_symbol:
        return {"success": False, "message": f"Broker symbol could not be resolved for ticker {ticker}."}

    symbol_info = mt5.symbol_info(broker_symbol)
    if symbol_info is None:
        return {"success": False, "message": f"Failed to get symbol info for {broker_symbol}."}

    action_clean = action.upper().strip()

    # Handle Position Closures
    if action_clean in ["CLOSE_LONG", "CLOSE_BUY"]:
        return close_mt5_positions(ticker=ticker, magic=magic, position_type="BUY")
    elif action_clean in ["CLOSE_SHORT", "CLOSE_SELL"]:
        return close_mt5_positions(ticker=ticker, magic=magic, position_type="SELL")
    elif action_clean in ["CLOSE", "CLOSE_ALL"]:
        return close_mt5_positions(ticker=ticker, magic=magic, position_type=None)

    # Handle Position Openings
    is_buy = action_clean in ["OPEN_LONG", "BUY"]
    is_sell = action_clean in ["OPEN_SHORT", "SELL"]

    if not is_buy and not is_sell:
        return {"success": False, "message": f"Invalid order action '{action}'."}

    # Normalize Lot Size within broker limits
    min_volume = symbol_info.volume_min
    max_volume = symbol_info.volume_max
    step_volume = symbol_info.volume_step
    vol = max(min_volume, min(max_volume, round(volume / step_volume) * step_volume))
    vol = round(vol, 2)

    tick = mt5.symbol_info_tick(broker_symbol)
    if tick is None:
        return {"success": False, "message": f"Failed to fetch market tick for {broker_symbol}."}

    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    price = tick.ask if is_buy else tick.bid

    # Calculate Stop Loss and Take Profit prices if specified
    sl_price = 0.0
    tp_price = 0.0
    if is_buy:
        if sl_pct > 0:
            sl_price = round(price * (1.0 - sl_pct / 100.0), symbol_info.digits)
        if tp_pct > 0:
            tp_price = round(price * (1.0 + tp_pct / 100.0), symbol_info.digits)
    else:  # SELL
        if sl_pct > 0:
            sl_price = round(price * (1.0 + sl_pct / 100.0), symbol_info.digits)
        if tp_pct > 0:
            tp_price = round(price * (1.0 - tp_pct / 100.0), symbol_info.digits)

    filling_mode = get_filling_mode(broker_symbol)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": broker_symbol,
        "volume": float(vol),
        "type": order_type,
        "price": float(price),
        "sl": float(sl_price),
        "tp": float(tp_price),
        "deviation": 20,
        "magic": int(magic),
        "comment": comment[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }

    result = mt5.order_send(request)
    if result is None:
        return {"success": False, "message": f"Order send failed completely: {mt5.last_error()}"}

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {
            "success": False,
            "retcode": result.retcode,
            "comment": result.comment,
            "message": f"MT5 Order Execution Rejected ({result.retcode}): {result.comment}",
        }

    print(
        f"[MT5 EXECUTOR SUCCESS] Direct Order Placed! Ticket #{result.order} | "
        f"{'BUY' if is_buy else 'SELL'} {vol} {broker_symbol} @ {result.price:.2f}"
    )

    return {
        "success": True,
        "ticket": result.order,
        "deal": result.deal,
        "volume": result.volume,
        "price": result.price,
        "bid": result.bid,
        "ask": result.ask,
        "comment": result.comment,
        "symbol": broker_symbol,
        "action": "BUY" if is_buy else "SELL",
        "message": f"Direct MT5 Order executed successfully! Ticket #{result.order}",
    }


def close_mt5_positions(
    ticker: str,
    magic: Optional[int] = DEFAULT_MAGIC_NUMBER,
    position_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Closes open MT5 positions for a specific ticker, optionally filtered by magic and position_type ('BUY' or 'SELL')."""
    if not initialize_mt5():
        return {"success": False, "message": "MT5 Terminal initialization failed."}

    broker_symbol = resolve_broker_symbol(ticker)
    if not broker_symbol:
        return {"success": False, "message": f"Broker symbol could not be resolved for {ticker}."}

    positions = mt5.positions_get(symbol=broker_symbol)
    if not positions:
        return {"success": True, "closed_count": 0, "message": f"No open MT5 positions found for {broker_symbol}."}

    closed_count = 0
    errors = []

    target_type = None
    if position_type:
        p_clean = position_type.upper().strip()
        if p_clean in ["BUY", "LONG"]:
            target_type = 0  # mt5.POSITION_TYPE_BUY
        elif p_clean in ["SELL", "SHORT"]:
            target_type = 1  # mt5.POSITION_TYPE_SELL

    for pos in positions:
        p_dict = pos._asdict()
        ticket = p_dict["ticket"]
        pos_type = p_dict["type"]
        pos_magic = p_dict.get("magic")
        vol = p_dict["volume"]

        # Filter by magic number if specified
        if magic and pos_magic != magic:
            continue

        # Filter by position type if specified (BUY=0, SELL=1)
        if target_type is not None and pos_type != target_type:
            continue

        # 0 is BUY (Close with SELL), 1 is SELL (Close with BUY)
        close_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(broker_symbol)
        if not tick:
            errors.append(f"Failed to get tick price for {broker_symbol}")
            continue

        price = tick.bid if pos_type == 0 else tick.ask
        filling_mode = get_filling_mode(broker_symbol)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": ticket,
            "symbol": broker_symbol,
            "volume": float(vol),
            "type": close_type,
            "price": float(price),
            "deviation": 20,
            "magic": int(magic) if magic else DEFAULT_MAGIC_NUMBER,
            "comment": f"MIMIR Close {'BUY' if pos_type == 0 else 'SELL'}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling_mode,
        }

        res = mt5.order_send(request)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            closed_count += 1
            print(f"[MT5 EXECUTOR SUCCESS] Closed {'BUY' if pos_type == 0 else 'SELL'} Position Ticket #{ticket} ({broker_symbol})")
        else:
            err_msg = res.comment if res else str(mt5.last_error())
            errors.append(f"Ticket #{ticket} close failed: {err_msg}")

    return {
        "success": closed_count > 0 or len(errors) == 0,
        "closed_count": closed_count,
        "errors": errors,
        "message": f"Closed {closed_count} {position_type or 'ALL'} MT5 position(s) for {broker_symbol}.",
    }
