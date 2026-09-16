# scripts/backtest_previous_alerts.py
import sys
import os
import re
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

PROJECT_ROOT = Path(r"e:\lion_stuff\Da Projects\MIMIR-new")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.database import get_db_connection_dict
from backend.app.config import get_settings
from backend.app.analytics.paper_trader import is_us_stock

settings = get_settings()

def run_alert_backtest():
    print("=" * 85)
    print("MIMIR ALERT BACKTEST: LEGACY VS. PHASE 1 VS. PHASE 2 INSTITUTIONAL")
    print("=" * 85)

    conn = get_db_connection_dict()
    cur = conn.cursor()

    # Query all historical trade alerts
    cur.execute(f"""
        SELECT s.id, s.ticker, s.signal_type, s.trigger_price, s.target_price, s.stop_loss,
               s.rsi_value, s.sentiment_score, s.catalyst_type, s.reason, s.created_at,
               s.conviction_score,
               COALESCE(p.optimal_hold_days, 5) as optimal_hold_days
        FROM {settings.mimir_schema}.mimir_trade_signals s
        LEFT JOIN {settings.mimir_schema}.mimir_ticker_parameters p ON s.ticker = p.ticker
        WHERE s.trigger_price IS NOT NULL AND s.trigger_price > 0
        ORDER BY s.created_at ASC
    """)
    alerts = cur.fetchall()
    print(f"Loaded {len(alerts)} historical trade alerts from {settings.mimir_schema}.mimir_trade_signals.")

    tickers = list(set([a['ticker'].strip().upper() for a in alerts if a['ticker'] and is_us_stock(a['ticker'])]))
    # Also ensure SPY and ^VIX are loaded
    macro_tickers = list(set(tickers + ['SPY', '^VIX', 'VIX']))

    cur.execute(f"""
        SELECT ticker, date, open, high, low, close 
        FROM {settings.mimir_schema}.v_mimir_daily_ohlcv
        WHERE ticker = ANY(%s)
        ORDER BY date ASC
    """, (macro_tickers,))
    ohlcv_rows = cur.fetchall()
    cur.close()
    conn.close()

    price_dict = {}
    for r in ohlcv_rows:
        t = r['ticker'].strip().upper()
        if t not in price_dict:
            price_dict[t] = []
        price_dict[t].append({
            'date': r['date'],
            'open': float(r['open']),
            'high': float(r['high']),
            'low': float(r['low']),
            'close': float(r['close'])
        })

    price_dfs = {t: pd.DataFrame(rows).set_index('date') for t, rows in price_dict.items()}
    print(f"Preloaded daily price history for {len(price_dfs)} tickers.")

    # 1. Precompute Macro Regimes (SPY 50-day SMA and VIX <= 25)
    spy_df = price_dfs.get('SPY')
    vix_df = price_dfs.get('^VIX')
    if vix_df is None:
        vix_df = price_dfs.get('VIX')

    spy_sma50 = {}
    if spy_df is not None and not spy_df.empty:
        spy_close = spy_df['close']
        sma50_series = spy_close.rolling(50, min_periods=20).mean()
        for d, c in spy_close.items():
            sma = sma50_series.get(d)
            # Favorable if SPY >= 50-SMA
            spy_sma50[d] = (c >= sma) if (sma and not np.isnan(sma)) else True

    vix_map = {}
    if vix_df is not None and not vix_df.empty:
        for d, c in vix_df['close'].items():
            vix_map[d] = c

    def is_macro_bullish_on_date(d):
        is_spy_ok = spy_sma50.get(d, True)
        vix_val = vix_map.get(d, 18.0)
        is_vix_ok = (vix_val <= 25.0) if vix_val else True
        return is_spy_ok and is_vix_ok

    # 2. Precalculate ATR per ticker
    atr_dict = {}
    for t, df in price_dfs.items():
        if len(df) >= 5:
            high = df['high']
            low = df['low']
            close = df['close']
            tr1 = high - low
            tr2 = (high - close.shift(1)).abs()
            tr3 = (low - close.shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr_val = float(tr.rolling(14, min_periods=5).mean().iloc[-1])
            atr_dict[t] = atr_val if not np.isnan(atr_val) and atr_val > 0 else None

    # Storage for simulation runs
    legacy_trades = []
    phase1_trades = []
    phase2_trades = []

    # Concurrency tracking for Phase 2 (tracking open dates to enforce max 5 concurrent holdings)
    active_positions_phase2 = []  # list of (exit_date, ticker)

    for alert in alerts:
        aid = alert['id']
        ticker = alert['ticker'].strip().upper()
        sig_type = alert['signal_type'].strip().upper()
        trigger_p = float(alert['trigger_price'])
        sig_date = alert['created_at'].date()
        reason = str(alert['reason'] or "")
        cat_type = str(alert['catalyst_type'] or "")
        sent = float(alert['sentiment_score']) if alert['sentiment_score'] is not None else 0.0
        if alert['conviction_score'] is not None:
            conviction = float(alert['conviction_score'])
        else:
            m = re.search(r'prob \((\d+\.?\d*)%\)', reason)
            if m:
                conviction = float(m.group(1)) / 100.0
            elif abs(sent) >= 0.20:
                conviction = 0.60 + abs(sent) * 0.2
            else:
                conviction = 0.50
        hold_days = int(alert['optimal_hold_days']) if alert['optimal_hold_days'] else 5

        if not is_us_stock(ticker):
            continue

        df_p = price_dfs.get(ticker)
        if df_p is None or df_p.empty:
            continue

        forward_bars = df_p[df_p.index >= sig_date].copy()
        if len(forward_bars) < 2:
            continue

        atr_val = atr_dict.get(ticker)
        if atr_val and trigger_p > 0:
            upd_sl_pct = max(3.5, min(7.5, (1.5 * atr_val / trigger_p) * 100.0))
            upd_tp_pct = max(6.0, min(15.0, (2.5 * atr_val / trigger_p) * 100.0))
        else:
            upd_sl_pct, upd_tp_pct = 4.5, 8.0

        if alert.get('stop_loss') and trigger_p > 0:
            custom_sl = abs(trigger_p - float(alert['stop_loss'])) / trigger_p * 100.0
            upd_sl_pct = max(3.5, min(7.5, custom_sl))
        if alert.get('target_price') and trigger_p > 0:
            custom_tp = abs(float(alert['target_price']) - trigger_p) / trigger_p * 100.0
            upd_tp_pct = max(6.0, min(15.0, custom_tp))
        upd_tp_pct = max(upd_tp_pct, round(upd_sl_pct * 1.67, 2))

        # -------------------------------------------------------------
        # 1. SCENARIO A: LEGACY RULES
        # -------------------------------------------------------------
        leg_sl = trigger_p * (0.97 if sig_type == 'BUY' else 1.03)
        leg_tp = trigger_p * (1.06 if sig_type == 'BUY' else 0.94)
        leg_exit_p = None
        leg_exit_reason = None
        leg_hold_bars = 0

        for i, (b_date, bar) in enumerate(forward_bars.iloc[1:].iterrows(), 1):
            leg_hold_bars = i
            if sig_type == 'BUY':
                if bar['low'] <= leg_sl:
                    leg_exit_p = leg_sl
                    leg_exit_reason = "STOP_LOSS"
                    break
                elif bar['high'] >= leg_tp:
                    leg_exit_p = leg_tp
                    leg_exit_reason = "TAKE_PROFIT"
                    break
            else:
                if bar['high'] >= leg_sl:
                    leg_exit_p = leg_sl
                    leg_exit_reason = "STOP_LOSS"
                    break
                elif bar['low'] <= leg_tp:
                    leg_exit_p = leg_tp
                    leg_exit_reason = "TAKE_PROFIT"
                    break

            if i >= hold_days:
                leg_exit_p = bar['open'] * (0.97 if sig_type == 'BUY' else 1.03)
                leg_exit_reason = "HOLD_EXPIRATION"
                break

        if leg_exit_p is None:
            leg_exit_p = forward_bars.iloc[-1]['close']
            leg_exit_reason = "HOLD_EXPIRATION"

        leg_pnl_pct = ((leg_exit_p - trigger_p) / trigger_p * 100.0) if sig_type == 'BUY' else ((trigger_p - leg_exit_p) / trigger_p * 100.0)
        legacy_trades.append({
            'id': aid,
            'ticker': ticker,
            'pnl_pct': leg_pnl_pct,
            'exit_reason': leg_exit_reason,
            'hold_days': leg_hold_bars,
            'is_win': leg_pnl_pct > 0
        })

        # -------------------------------------------------------------
        # 2. SCENARIO B: PHASE 1 STRATEGY
        # -------------------------------------------------------------
        is_rsi_signal = "Extreme oversold reversion" in reason or "Extreme overbought reversion" in reason
        p1_filter = False
        if is_rsi_signal:
            if (sig_type == 'BUY' and sent < 0.20) or (sig_type == 'SELL' and sent > -0.20):
                p1_filter = True
        if "PEAK Narrative" in reason or "FADING Narrative" in reason:
            p1_filter = True

        if not p1_filter:
            p1_sl = trigger_p * (1.0 - upd_sl_pct / 100.0) if sig_type == 'BUY' else trigger_p * (1.0 + upd_sl_pct / 100.0)
            p1_tp = trigger_p * (1.0 + upd_tp_pct / 100.0) if sig_type == 'BUY' else trigger_p * (1.0 - upd_tp_pct / 100.0)
            p1_exit_p = None
            p1_exit_reason = None
            p1_hold_bars = 0

            for i, (b_date, bar) in enumerate(forward_bars.iloc[1:].iterrows(), 1):
                p1_hold_bars = i
                if sig_type == 'BUY':
                    if bar['low'] <= p1_sl:
                        p1_exit_p = p1_sl
                        p1_exit_reason = "STOP_LOSS"
                        break
                    elif bar['high'] >= p1_tp:
                        p1_exit_p = p1_tp
                        p1_exit_reason = "TAKE_PROFIT"
                        break
                else:
                    if bar['high'] >= p1_sl:
                        p1_exit_p = p1_sl
                        p1_exit_reason = "STOP_LOSS"
                        break
                    elif bar['low'] <= p1_tp:
                        p1_exit_p = p1_tp
                        p1_exit_reason = "TAKE_PROFIT"
                        break

                if i >= hold_days:
                    p1_exit_p = bar['close']
                    p1_exit_reason = "HOLD_EXPIRATION"
                    break

            if p1_exit_p is None:
                p1_exit_p = forward_bars.iloc[-1]['close']
                p1_exit_reason = "HOLD_EXPIRATION"

            p1_pnl_pct = ((p1_exit_p - trigger_p) / trigger_p * 100.0) if sig_type == 'BUY' else ((trigger_p - p1_exit_p) / trigger_p * 100.0)
            phase1_trades.append({
                'id': aid,
                'ticker': ticker,
                'pnl_pct': p1_pnl_pct,
                'exit_reason': p1_exit_reason,
                'hold_days': p1_hold_bars,
                'is_win': p1_pnl_pct > 0
            })

        # -------------------------------------------------------------
        # 3. SCENARIO C: PHASE 2 INSTITUTIONAL STRATEGY (All Qualified Signals)
        # Upgrades:
        #   1. Macro Regime Gate (SPY >= 50-SMA and VIX <= 25)
        #   2. High-Conviction Floor (conviction >= 0.65)
        #   3. Dynamic Breakeven Stop Ratchet (move SL to +0.5% when gain >= +3.0%)
        # -------------------------------------------------------------
        p2_filter = p1_filter

        # Macro Gate
        if sig_type == 'BUY' and not is_macro_bullish_on_date(sig_date):
            p2_filter = True

        # High Conviction Floor
        if conviction < 0.65 and cat_type not in ["PRE_EARNINGS_BEAT", "SUPPLY_CHAIN_SPILLOVER"]:
            p2_filter = True

        if not p2_filter:
            p2_sl = trigger_p * (1.0 - upd_sl_pct / 100.0) if sig_type == 'BUY' else trigger_p * (1.0 + upd_sl_pct / 100.0)
            p2_tp = trigger_p * (1.0 + upd_tp_pct / 100.0) if sig_type == 'BUY' else trigger_p * (1.0 - upd_tp_pct / 100.0)
            p2_exit_p = None
            p2_exit_reason = None
            p2_hold_bars = 0
            be_ratchet_active = False

            for i, (b_date, bar) in enumerate(forward_bars.iloc[1:].iterrows(), 1):
                p2_hold_bars = i

                # Dynamic Breakeven Stop Ratchet:
                # If trade reaches +3.0% gain, ratchet SL to Breakeven (+0.5%)
                if sig_type == 'BUY':
                    if not be_ratchet_active and bar['high'] >= trigger_p * 1.03:
                        be_ratchet_active = True
                        p2_sl = max(p2_sl, trigger_p * 1.005)

                    if bar['low'] <= p2_sl:
                        p2_exit_p = p2_sl
                        p2_exit_reason = "BREAKEVEN_STOP" if be_ratchet_active else "STOP_LOSS"
                        break
                    elif bar['high'] >= p2_tp:
                        p2_exit_p = p2_tp
                        p2_exit_reason = "TAKE_PROFIT"
                        break
                else:
                    if not be_ratchet_active and bar['low'] <= trigger_p * 0.97:
                        be_ratchet_active = True
                        p2_sl = min(p2_sl, trigger_p * 0.995)

                    if bar['high'] >= p2_sl:
                        p2_exit_p = p2_sl
                        p2_exit_reason = "BREAKEVEN_STOP" if be_ratchet_active else "STOP_LOSS"
                        break
                    elif bar['low'] <= p2_tp:
                        p2_exit_p = p2_tp
                        p2_exit_reason = "TAKE_PROFIT"
                        break

                if i >= hold_days:
                    p2_exit_p = bar['close']
                    p2_exit_reason = "HOLD_EXPIRATION"
                    break

            if p2_exit_p is None:
                p2_exit_p = forward_bars.iloc[-1]['close']
                p2_exit_reason = "HOLD_EXPIRATION"

            p2_pnl_pct = ((p2_exit_p - trigger_p) / trigger_p * 100.0) if sig_type == 'BUY' else ((trigger_p - p2_exit_p) / trigger_p * 100.0)
            phase2_trades.append({
                'id': aid,
                'ticker': ticker,
                'pnl_pct': p2_pnl_pct,
                'exit_reason': p2_exit_reason,
                'hold_days': p2_hold_bars,
                'is_win': p2_pnl_pct > 0,
                'conviction': conviction,
                'sig_date': sig_date,
                'sig_type': sig_type,
                'trigger_p': trigger_p,
                'upd_sl_pct': upd_sl_pct,
                'upd_tp_pct': upd_tp_pct,
                'hold_days_limit': hold_days,
                'forward_bars': forward_bars
            })

    # -------------------------------------------------------------
    # 4. SCENARIO D: PHASE 2 INSTITUTIONAL PORTFOLIO (Cap = 5 Positions)
    # Upgrades:
    #   - Max 5 concurrent open positions
    #   - Max 3 new admissions per day
    #   - Daily priority ranking by Conviction Score (highest conviction first)
    #   - Dynamic Breakeven Stop Ratchet
    # -------------------------------------------------------------
    phase2_portfolio_trades = []
    by_date = {}
    for t_cand in phase2_trades:
        d = t_cand['sig_date']
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(t_cand)

    # Sort each day by conviction score descending
    for d in by_date:
        by_date[d].sort(key=lambda x: x['conviction'], reverse=True)

    open_portfolio = []  # list of {ticker, exit_date, pnl_pct, exit_reason, is_win, hold_days}
    max_portfolio_concurrency = 5
    max_new_per_day = 3

    for d in sorted(by_date.keys()):
        # Expire positions that closed on or before date d
        still_open = []
        for pos in open_portfolio:
            if pos['exit_date'] <= d:
                phase2_portfolio_trades.append(pos)
            else:
                still_open.append(pos)
        open_portfolio = still_open

        available_slots = max_portfolio_concurrency - len(open_portfolio)
        if available_slots <= 0:
            continue

        open_tickers = set(p['ticker'] for p in open_portfolio)
        admitted = 0

        for cand in by_date[d]:
            if cand['ticker'] in open_tickers:
                continue
            if admitted >= available_slots or admitted >= max_new_per_day:
                break

            # Simulate portfolio trade
            trigger_p = cand['trigger_p']
            sig_type = cand['sig_type']
            upd_sl_pct = cand['upd_sl_pct']
            upd_tp_pct = cand['upd_tp_pct']
            hold_days = cand['hold_days_limit']
            f_bars = cand['forward_bars']

            p_sl = trigger_p * (1.0 - upd_sl_pct / 100.0) if sig_type == 'BUY' else trigger_p * (1.0 + upd_sl_pct / 100.0)
            p_tp = trigger_p * (1.0 + upd_tp_pct / 100.0) if sig_type == 'BUY' else trigger_p * (1.0 - upd_tp_pct / 100.0)
            p_exit_p = None
            p_exit_reason = None
            p_hold_bars = 0
            be_active = False

            for i, (b_date, bar) in enumerate(f_bars.iloc[1:].iterrows(), 1):
                p_hold_bars = i
                if sig_type == 'BUY':
                    if not be_active and bar['high'] >= trigger_p * 1.03:
                        be_active = True
                        p_sl = max(p_sl, trigger_p * 1.005)
                    if bar['low'] <= p_sl:
                        p_exit_p = p_sl
                        p_exit_reason = "BREAKEVEN_STOP" if be_active else "STOP_LOSS"
                        break
                    elif bar['high'] >= p_tp:
                        p_exit_p = p_tp
                        p_exit_reason = "TAKE_PROFIT"
                        break
                else:
                    if not be_active and bar['low'] <= trigger_p * 0.97:
                        be_active = True
                        p_sl = min(p_sl, trigger_p * 0.995)
                    if bar['high'] >= p_sl:
                        p_exit_p = p_sl
                        p_exit_reason = "BREAKEVEN_STOP" if be_active else "STOP_LOSS"
                        break
                    elif bar['low'] <= p_tp:
                        p_exit_p = p_tp
                        p_exit_reason = "TAKE_PROFIT"
                        break

                if i >= hold_days:
                    p_exit_p = bar['close']
                    p_exit_reason = "HOLD_EXPIRATION"
                    break

            if p_exit_p is None:
                p_exit_p = f_bars.iloc[-1]['close']
                p_exit_reason = "HOLD_EXPIRATION"

            p_pnl = ((p_exit_p - trigger_p) / trigger_p * 100.0) if sig_type == 'BUY' else ((trigger_p - p_exit_p) / trigger_p * 100.0)
            p_exit_date = f_bars.index[min(p_hold_bars, len(f_bars) - 1)]

            open_portfolio.append({
                'id': cand['id'],
                'ticker': cand['ticker'],
                'pnl_pct': p_pnl,
                'exit_reason': p_exit_reason,
                'hold_days': p_hold_bars,
                'is_win': p_pnl > 0,
                'exit_date': p_exit_date
            })
            open_tickers.add(cand['ticker'])
            admitted += 1

    phase2_portfolio_trades.extend(open_portfolio)

    # Summary Statistics Calculation
    def compute_stats(trades_list, name):
        df = pd.DataFrame(trades_list)
        if df.empty:
            return {}
        n = len(df)
        wins = df[df['pnl_pct'] > 0]
        losses = df[df['pnl_pct'] < 0]
        ties = df[df['pnl_pct'] == 0]
        win_rate = (len(wins) / n * 100.0)
        total_pnl = df['pnl_pct'].sum()
        avg_trade = df['pnl_pct'].mean()
        avg_win = wins['pnl_pct'].mean() if len(wins) > 0 else 0.0
        avg_loss = losses['pnl_pct'].mean() if len(losses) > 0 else 0.0
        gross_profit = wins['pnl_pct'].sum()
        gross_loss = abs(losses['pnl_pct'].sum())
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else np.nan
        payoff = (avg_win / abs(avg_loss)) if abs(avg_loss) > 0 else np.nan
        expectancy = (win_rate / 100.0 * avg_win) + ((1 - win_rate / 100.0) * avg_loss)

        cum = (1 + df['pnl_pct'] / 100.0).cumprod()
        peak = cum.cummax()
        max_dd = ((cum - peak) / peak).min() * 100.0

        return {
            "Strategy": name,
            "Total Trades": n,
            "Wins": len(wins),
            "Losses": len(losses),
            "Win Rate (%)": round(win_rate, 2),
            "Total Return (%)": round(total_pnl, 2),
            "Avg Trade (%)": round(avg_trade, 2),
            "Avg Win (%)": round(avg_win, 2),
            "Avg Loss (%)": round(avg_loss, 2),
            "Profit Factor": round(profit_factor, 2),
            "Payoff Ratio": round(payoff, 2),
            "Expectancy (%)": round(expectancy, 2),
            "Max DD (%)": round(max_dd, 2)
        }

    s_leg = compute_stats(legacy_trades, "1. Legacy Strategy (Pre-Update)")
    s_p1 = compute_stats(phase1_trades, "2. Phase 1 Strategy (Initial 4 Updates)")
    s_p2_all = compute_stats(phase2_trades, "3. Phase 2 Institutional (All Qualified Signals)")
    s_p2_port = compute_stats(phase2_portfolio_trades, "4. Phase 2 Institutional Portfolio (Cap=5 Positions)")

    df_comp = pd.DataFrame([s_leg, s_p1, s_p2_all, s_p2_port])

    print("\n" + "=" * 125)
    print("MIMIR EVOLUTION BENCHMARK MATRIX (Historical Alerts 2025-2026)")
    print("=" * 125)
    print(df_comp.to_string(index=False))

    df_port = pd.DataFrame(phase2_portfolio_trades)
    print("\n" + "=" * 70)
    print("PHASE 2 INSTITUTIONAL PORTFOLIO (CAP=5): EXIT REASONS BREAKDOWN")
    print("=" * 70)
    print(df_port.groupby('exit_reason').agg(
        trades=('id', 'count'),
        win_rate=('is_win', lambda x: round(x.mean() * 100, 1)),
        avg_pnl=('pnl_pct', lambda x: round(x.mean(), 2)),
        total_pnl=('pnl_pct', lambda x: round(x.sum(), 2))
    ).to_string())

    # Save to CSV
    csv_out = Path(PROJECT_ROOT, "scratch_institutional_alerts_backtest.csv")
    df_port.to_csv(csv_out, index=False)
    print(f"\nDetailed Phase 2 portfolio backtest saved to {csv_out}")

if __name__ == '__main__':
    run_alert_backtest()

