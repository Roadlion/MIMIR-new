# backend/app/analytics/mt5_bridge.py
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

# Reentrant lock to serialize MT5 order and state requests
_mt5_lock = threading.RLock()

# Default magic number for MIMIR trades
MIMIR_MAGIC = 202409

# Human-readable translations for MT5 trade return codes
MT5_RETCODE_DESCRIPTIONS = {
    10004: "Requote",
    10006: "Request rejected",
    10007: "Request canceled by trader",
    10008: "Order placed",
    10009: "Order executed successfully (Done)",
    10010: "Only part of request completed",
    10011: "Request processing error",
    10012: "Request timed out",
    10013: "Invalid request format",
    10014: "Invalid volume / lot size",
    10015: "Invalid price",
    10016: "Invalid stops (SL or TP too close to price)",
    10017: "Trade is disabled by broker",
    10018: "Market is closed for this symbol",
    10019: "Insufficient funds / not enough free margin",
    10020: "Prices changed",
    10021: "No quotes available for symbol",
    10022: "Order expiration date invalid",
    10023: "Order state changed",
    10024: "Too many frequent requests",
    10025: "No changes in order",
    10026: "Autotrading disabled by broker server",
    10027: "AutoTrading disabled by client. Please enable 'Algo Trading' in MT5 (Ctrl+E)",
    10028: "Request locked for processing",
    10029: "Order or position frozen",
    10030: "Unsupported order filling mode",
    10031: "No connection with the trade server",
    10032: "Operation allowed only for live accounts",
    10033: "The number of pending orders has reached the limit",
    10034: "Volume of orders and positions for symbol has reached limit",
}


def ensure_mt5_connected() -> bool:
    if mt5 is None:
        return False
    with _mt5_lock:
        try:
            # Check existing connection first
            term_info = mt5.terminal_info()
            if term_info and term_info.connected:
                return True
            
            # Initialize connection to active/default terminal
            if mt5.initialize():
                return True
            return False
        except Exception as e:
            print(f"[MT5_BRIDGE ERROR] Failed to connect to MT5: {e}")
            return False


def get_terminal_and_account_status() -> Dict[str, Any]:
    """
    Returns live terminal connection details, account info,
    and whether automated trading (trade_allowed) is toggled ON.
    """
    with _mt5_lock:
        if not ensure_mt5_connected():
            return {
                "connected": False,
                "error": "Cannot connect to MetaTrader 5 terminal. Ensure MT5 is running.",
                "trade_allowed": False,
                "login": None,
                "server": None,
                "company": None,
                "balance": 0.0,
                "equity": 0.0,
                "margin_free": 0.0
            }

        try:
            t_info = mt5.terminal_info()
            acc = mt5.account_info()

            if not acc:
                return {
                    "connected": True,
                    "error": "Connected to MT5 terminal, but no trading account is logged in.",
                    "trade_allowed": False,
                    "login": None,
                    "server": None,
                    "balance": 0.0,
                    "equity": 0.0,
                    "margin_free": 0.0
                }

            acc_dict = acc._asdict()
            term_dict = t_info._asdict() if t_info else {}

            # Algo trading is allowed only if BOTH terminal and account permit it
            terminal_algo = term_dict.get("trade_allowed", False)
            account_trade = acc_dict.get("trade_allowed", False) and acc_dict.get("trade_expert", False)
            is_trade_allowed = bool(terminal_algo and account_trade)

            algo_status_msg = (
                "READY" if is_trade_allowed else 
                ("ALGO_DISABLED_TERMINAL" if not terminal_algo else "ACCOUNT_TRADE_DISALLOWED")
            )

            return {
                "connected": True,
                "trade_allowed": is_trade_allowed,
                "algo_status": algo_status_msg,
                "terminal_name": term_dict.get("name", "MetaTrader 5"),
                "login": acc_dict.get("login"),
                "name": acc_dict.get("name"),
                "server": acc_dict.get("server"),
                "currency": acc_dict.get("currency", "USD"),
                "company": acc_dict.get("company"),
                "balance": float(acc_dict.get("balance", 0.0)),
                "equity": float(acc_dict.get("equity", 0.0)),
                "margin": float(acc_dict.get("margin", 0.0)),
                "margin_free": float(acc_dict.get("margin_free", 0.0)),
                "margin_level": float(acc_dict.get("margin_level", 0.0)),
                "leverage": acc_dict.get("leverage", 100),
                "profit": float(acc_dict.get("profit", 0.0))
            }
        except Exception as e:
            return {
                "connected": False,
                "error": f"Error querying MT5 account status: {e}",
                "trade_allowed": False,
                "balance": 0.0,
                "equity": 0.0,
                "margin_free": 0.0
            }


_symbol_cache: Dict[str, str] = {}

def resolve_mt5_symbol(ticker: str) -> Optional[str]:
    """
    Resolves standard ticker (e.g. 'AAPL', 'NVDA', 'EURUSD', 'BTCUSD')
    to broker-specific symbol in MT5 Market Watch (e.g. 'AAPL.US', '#AAPL', etc.).
    """
    if not ticker:
        return None

    clean = ticker.strip().upper()
    # Check cache first
    if clean in _symbol_cache:
        return _symbol_cache[clean]

    candidates = [
        clean,
        f"{clean}.US",
        f"#{clean}",
        clean.replace(".", ""),
        f"{clean.replace('.', '')}.US"
    ]
    if clean in ("BTCUSD", "BTC-USD", "BTC"):
        candidates.extend(["BTCUSD", "BTC", "BTC.USD", "BITCOIN"])

    with _mt5_lock:
        if not ensure_mt5_connected():
            return None

        for cand in candidates:
            try:
                # Attempt to select into Market Watch
                if mt5.symbol_select(cand, True):
                    info = mt5.symbol_info(cand)
                    if info is not None:
                        _symbol_cache[clean] = cand
                        return cand
            except Exception:
                continue

    return None


def get_supported_filling_modes(symbol_info) -> List[int]:
    """
    Determines supported MT5 execution filling modes from symbol_info.filling_mode bitmask.
    bit 0 (1): FOK (ORDER_FILLING_FOK)
    bit 1 (2): IOC (ORDER_FILLING_IOC)
    Else: RETURN (ORDER_FILLING_RETURN)
    """
    modes = []
    fm = getattr(symbol_info, "filling_mode", 0)
    if fm & 1:
        modes.append(mt5.ORDER_FILLING_FOK)
    if fm & 2:
        modes.append(mt5.ORDER_FILLING_IOC)
    # Always include RETURN as fallback
    if mt5.ORDER_FILLING_RETURN not in modes:
        modes.append(mt5.ORDER_FILLING_RETURN)
    if not modes:
        modes = [mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN]
    return modes


def calculate_mt5_volume(symbol_info, target_usd: float, price: float, fixed_qty: Optional[float] = None) -> float:
    """
    Calculates compliant volume (lots/shares) matching broker min, max, and step requirements.
    """
    vol_min = symbol_info.volume_min or 1.0
    vol_max = symbol_info.volume_max or 100000.0
    vol_step = symbol_info.volume_step or 1.0
    contract_size = symbol_info.trade_contract_size or 1.0

    if fixed_qty is not None and fixed_qty > 0:
        raw_vol = float(fixed_qty)
    else:
        if price <= 0 or target_usd <= 0:
            return vol_min
        raw_vol = target_usd / (price * contract_size)

    # Step quantization
    steps = round(raw_vol / vol_step)
    vol = steps * vol_step
    vol = max(vol_min, min(vol_max, vol))

    # Determine decimal places of step
    step_str = str(vol_step)
    decimals = len(step_str.split(".")[1]) if "." in step_str else 0
    return round(vol, decimals)


def send_market_order(
    ticker: str,
    action: str,
    target_usd: float = 200.0,
    fixed_quantity: Optional[float] = None,
    sl_pct: Optional[float] = None,
    tp_pct: Optional[float] = None,
    comment: str = "",
    magic: int = MIMIR_MAGIC
) -> Dict[str, Any]:
    """
    Places a live market BUY or SELL deal in the connected MT5 account.
    Attaches Stop Loss and Take Profit levels according to sl_pct and tp_pct.
    """
    with _mt5_lock:
        if not ensure_mt5_connected():
            return {
                "success": False,
                "message": "Cannot execute order: MT5 terminal is not connected.",
                "retcode": -1
            }

        t_info = mt5.terminal_info()
        if not t_info or not t_info.trade_allowed:
            return {
                "success": False,
                "message": (
                    "MT5 AutoTrading is disabled in your MT5 terminal. "
                    "Please click the 'Algo Trading' button in MetaTrader 5 (or press Ctrl+E) to allow automated execution."
                ),
                "retcode": 10027
            }

        b_symbol = resolve_mt5_symbol(ticker)
        if not b_symbol:
            return {
                "success": False,
                "message": f"Symbol '{ticker}' could not be resolved or selected in MT5 Market Watch.",
                "retcode": 10013
            }

        sym_info = mt5.symbol_info(b_symbol)
        if not sym_info:
            return {
                "success": False,
                "message": f"Could not retrieve symbol info for '{b_symbol}'.",
                "retcode": 10013
            }

        tick = mt5.symbol_info_tick(b_symbol)
        if not tick:
            return {
                "success": False,
                "message": f"No live price tick available for '{b_symbol}'. Market may be closed.",
                "retcode": 10021
            }

        act_upper = action.strip().upper()
        if act_upper not in ("BUY", "SELL"):
            return {
                "success": False,
                "message": f"Invalid action '{action}'. Must be BUY or SELL.",
                "retcode": 10013
            }

        is_buy = (act_upper == "BUY")
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid

        if price <= 0:
            return {
                "success": False,
                "message": f"Invalid market quote for '{b_symbol}' (price = {price}).",
                "retcode": 10015
            }

        volume = calculate_mt5_volume(sym_info, target_usd, price, fixed_qty=fixed_quantity)
        digits = sym_info.digits or 2

        # Calculate SL / TP prices
        sl_price = 0.0
        tp_price = 0.0

        if sl_pct and sl_pct > 0:
            if is_buy:
                sl_price = round(price * (1.0 - (sl_pct / 100.0)), digits)
            else:
                sl_price = round(price * (1.0 + (sl_pct / 100.0)), digits)

        if tp_pct and tp_pct > 0:
            if is_buy:
                tp_price = round(price * (1.0 + (tp_pct / 100.0)), digits)
            else:
                tp_price = round(price * (1.0 - (tp_pct / 100.0)), digits)

        # Truncate comment to MT5 limit (max 31 characters)
        order_comment = (comment or f"MIMIR:{ticker}")[:31]

        filling_modes = get_supported_filling_modes(sym_info)
        last_result = None

        for fm in filling_modes:
            trade_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": b_symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": sl_price,
                "tp": tp_price,
                "deviation": 20,
                "magic": magic,
                "comment": order_comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fm,
            }

            # Pre-check request
            check_res = mt5.order_check(trade_request)
            if check_res and check_res.retcode == 10030:
                # Unsupported filling mode, continue to next candidate
                continue

            order_res = mt5.order_send(trade_request)
            if order_res is None:
                last_err = mt5.last_error()
                return {
                    "success": False,
                    "message": f"MT5 order_send returned None. Error: {last_err}",
                    "retcode": -1
                }

            last_result = order_res
            if order_res.retcode == mt5.TRADE_RETCODE_DONE:
                # Success!
                return {
                    "success": True,
                    "message": f"Executed {act_upper} {volume} lots of {ticker} in MT5 at ${order_res.price:.2f}.",
                    "retcode": order_res.retcode,
                    "order_id": order_res.order,
                    "deal_id": order_res.deal,
                    "ticket": order_res.order,
                    "volume": order_res.volume,
                    "price": order_res.price,
                    "symbol": b_symbol,
                    "ticker": ticker,
                    "action": act_upper,
                    "sl": sl_price,
                    "tp": tp_price,
                    "comment": order_comment
                }
            elif order_res.retcode == 10030:
                # Try next filling mode
                continue
            else:
                # Break on real broker error
                break

        ret_code = last_result.retcode if last_result else -1
        desc = MT5_RETCODE_DESCRIPTIONS.get(ret_code, last_result.comment if last_result else "Order failed")
        return {
            "success": False,
            "message": f"MT5 order rejected: {desc} (code {ret_code})",
            "retcode": ret_code,
            "comment": getattr(last_result, "comment", "")
        }


def close_position(ticket: Optional[int] = None, ticker: Optional[str] = None) -> Dict[str, Any]:
    """
    Closes an active position in MT5 by ticket or ticker.
    Sends an opposite DEAL order targeting the position ticket.
    """
    with _mt5_lock:
        if not ensure_mt5_connected():
            return {"success": False, "message": "MT5 terminal is not connected."}

        positions = mt5.positions_get()
        if not positions:
            return {"success": False, "message": "No active open positions found in MT5."}

        target_pos = None
        if ticket is not None:
            for p in positions:
                if p.ticket == ticket:
                    target_pos = p
                    break
        elif ticker:
            clean = ticker.strip().upper()
            for p in positions:
                resolved = resolve_mt5_symbol(clean)
                if p.symbol.upper() in (clean, f"{clean}.US", f"#{clean}") or (resolved and p.symbol == resolved):
                    target_pos = p
                    break

        if not target_pos:
            ident = f"#{ticket}" if ticket else ticker
            return {"success": False, "message": f"Position {ident} not found in MT5 open positions."}

        sym_info = mt5.symbol_info(target_pos.symbol)
        tick = mt5.symbol_info_tick(target_pos.symbol)
        if not tick or not sym_info:
            return {"success": False, "message": f"Cannot get live tick for {target_pos.symbol} to close position."}

        # Determine opposite order type
        is_buy = (target_pos.type == mt5.ORDER_TYPE_BUY)
        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if is_buy else tick.ask

        filling_modes = get_supported_filling_modes(sym_info)
        last_res = None

        for fm in filling_modes:
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": target_pos.ticket,
                "symbol": target_pos.symbol,
                "volume": target_pos.volume,
                "type": close_type,
                "price": close_price,
                "deviation": 20,
                "magic": target_pos.magic,
                "comment": f"MIMIR:Close #{target_pos.ticket}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": fm,
            }

            res = mt5.order_send(req)
            if res is None:
                continue
            last_res = res
            if res.retcode == mt5.TRADE_RETCODE_DONE:
                return {
                    "success": True,
                    "message": f"Successfully closed position #{target_pos.ticket} for {target_pos.symbol} at ${close_price:.2f}.",
                    "ticket": target_pos.ticket,
                    "symbol": target_pos.symbol,
                    "volume": target_pos.volume,
                    "close_price": close_price,
                    "profit": target_pos.profit
                }
            elif res.retcode == 10030:
                continue
            else:
                break

        ret_code = last_res.retcode if last_res else -1
        desc = MT5_RETCODE_DESCRIPTIONS.get(ret_code, last_res.comment if last_res else "Close failed")
        return {
            "success": False,
            "message": f"MT5 close rejected: {desc} (code {ret_code})",
            "retcode": ret_code
        }


def close_all_positions(magic_only: Optional[int] = None) -> Dict[str, Any]:
    """
    Closes all active open positions in MT5 (optionally filtered by magic number).
    """
    with _mt5_lock:
        if not ensure_mt5_connected():
            return {"success": False, "message": "MT5 terminal is not connected."}

        positions = mt5.positions_get()
        if not positions:
            return {"success": True, "closed_count": 0, "message": "No active open positions to close."}

        closed_count = 0
        errors = []

        for p in positions:
            if magic_only is not None and p.magic != magic_only:
                continue
            res = close_position(ticket=p.ticket)
            if res.get("success"):
                closed_count += 1
            else:
                errors.append(f"#{p.ticket} ({p.symbol}): {res.get('message')}")

        return {
            "success": True,
            "closed_count": closed_count,
            "errors": errors,
            "message": f"Closed {closed_count} position(s) in MT5."
        }


def modify_position_sltp(ticket: int, sl: Optional[float] = None, tp: Optional[float] = None) -> Dict[str, Any]:
    """Modifies Stop Loss and Take Profit levels for an open MT5 position."""
    with _mt5_lock:
        if not ensure_mt5_connected():
            return {"success": False, "message": "MT5 terminal is not connected."}

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return {"success": False, "message": f"Position #{ticket} not found in MT5."}

        pos = positions[0]
        sym_info = mt5.symbol_info(pos.symbol)
        digits = sym_info.digits if sym_info else 2

        new_sl = round(sl, digits) if (sl is not None and sl > 0) else pos.sl
        new_tp = round(tp, digits) if (tp is not None and tp > 0) else pos.tp

        req = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "sl": new_sl,
            "tp": new_tp,
        }

        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            return {
                "success": True,
                "message": f"Updated SL (${new_sl}) and TP (${new_tp}) for position #{ticket}.",
                "ticket": ticket,
                "sl": new_sl,
                "tp": new_tp
            }

        ret_code = res.retcode if res else -1
        desc = MT5_RETCODE_DESCRIPTIONS.get(ret_code, res.comment if res else "Modification failed")
        return {
            "success": False,
            "message": f"Failed to modify position: {desc} (code {ret_code})",
            "retcode": ret_code
        }


def get_open_positions(magic_only: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Retrieves all open positions from MT5 with live market values and P&L.
    """
    with _mt5_lock:
        if not ensure_mt5_connected():
            return []

        positions = mt5.positions_get()
        if not positions:
            return []

        result = []
        for p in positions:
            if magic_only is not None and p.magic != magic_only:
                continue

            sym_info = mt5.symbol_info(p.symbol)
            contract_size = sym_info.trade_contract_size if sym_info else 1.0

            action = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
            open_cost = p.volume * p.price_open * contract_size
            curr_value = p.volume * p.price_current * contract_size

            # Profit percentage
            if open_cost > 0:
                pnl_pct = (p.profit / open_cost) * 100.0
            else:
                pnl_pct = 0.0

            open_time_str = datetime.fromtimestamp(p.time).strftime("%Y-%m-%d %H:%M:%S")

            clean_ticker = p.symbol.replace(".US", "").replace("#", "")

            result.append({
                "ticket": p.ticket,
                "ticker": clean_ticker,
                "mt5_symbol": p.symbol,
                "action": action,
                "quantity": p.volume,
                "avg_entry_price": float(p.price_open),
                "current_price": float(p.price_current),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "total_cost": float(open_cost),
                "current_value": float(curr_value),
                "unrealized_pnl": float(p.profit),
                "unrealized_pnl_pct": float(pnl_pct),
                "open_time": open_time_str,
                "magic": p.magic,
                "comment": p.comment
            })

        return result


def get_closed_deals(days: int = 90, magic_only: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Queries MT5 deal history and retrieves closed trades (DEAL_ENTRY_OUT / DEAL_ENTRY_INOUT)
    with realized P&L, commission, swap, exit prices, and timestamp.
    """
    with _mt5_lock:
        if not ensure_mt5_connected():
            return []

        from_date = datetime.now() - timedelta(days=days)
        to_date = datetime.now() + timedelta(days=1)

        deals = mt5.history_deals_get(from_date, to_date)
        if not deals:
            return []

        result = []
        for d in reversed(deals):
            # Skip non-trade balance operations or opening entries
            # We want closed trade results: DEAL_ENTRY_OUT (1) or DEAL_ENTRY_INOUT (2)
            if d.entry not in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT):
                continue
            if magic_only is not None and d.magic != magic_only:
                continue

            action = "SELL" if d.type == 1 else "BUY"
            clean_ticker = d.symbol.replace(".US", "").replace("#", "")
            deal_time_str = datetime.fromtimestamp(d.time).strftime("%Y-%m-%d %H:%M:%S")

            net_pnl = float(d.profit) + float(d.commission) + float(d.swap)

            result.append({
                "id": d.ticket,
                "deal_id": d.ticket,
                "order_id": d.order,
                "position_id": d.position_id,
                "ticker": clean_ticker,
                "mt5_symbol": d.symbol,
                "action": action,
                "quantity": float(d.volume),
                "entry_price": 0.0,  # MT5 deal records exit price
                "exit_price": float(d.price),
                "realized_pnl": float(d.profit),
                "commission": float(d.commission),
                "swap": float(d.swap),
                "net_pnl": net_pnl,
                "exit_reason": d.comment or "MT5_EXIT",
                "exit_time": deal_time_str,
                "notes": f"MT5 Deal #{d.ticket} (Position #{d.position_id})"
            })

        return result
