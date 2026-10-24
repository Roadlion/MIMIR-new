# scripts/test_wq_operators.py
import os
import sys
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)

from backend.app.analytics.expression_parser import FormulaParser

def test_new_operators():
    print("--- Testing New WorldQuant Brain Operators ---")
    dates = pd.to_datetime(['2025-01-01', '2025-01-02', '2025-01-03', '2025-01-04'])
    tickers = ['AAPL', 'MSFT']
    
    close_df = pd.DataFrame([
        [150.0, 300.0],
        [152.0, 298.0],
        [155.0, 305.0],
        [151.0, 301.0]
    ], index=dates, columns=tickers)
    
    sentiment_df = pd.DataFrame([
        [0.5, -0.2],
        [0.6, -0.5],
        [0.2, 0.8],
        [-0.1, 0.3]
    ], index=dates, columns=tickers)
    
    data = {
        'close': close_df,
        'sentiment': sentiment_df
    }
    
    parser = FormulaParser(data)
    
    # 1. Unary Math Operators
    print("Testing ceil, floor, round, sigmoid, tanh, s_log_1p...")
    res_ceil = parser.evaluate("ceil(close)")
    assert res_ceil.equals(close_df), "ceil failed"
    
    res_floor = parser.evaluate("floor(close)")
    assert res_floor.equals(close_df), "floor failed"
    
    res_round = parser.evaluate("round(close)")
    assert res_round.equals(close_df), "round failed"
    
    res_sigmoid = parser.evaluate("sigmoid(sentiment)")
    assert 0.5 < res_sigmoid.loc[dates[0], 'AAPL'] < 1.0, "sigmoid failed"
    
    res_tanh = parser.evaluate("tanh(sentiment)")
    assert 0.0 < res_tanh.loc[dates[0], 'AAPL'] < 1.0, "tanh failed"
    
    res_slog = parser.evaluate("s_log_1p(sentiment)")
    assert res_slog.loc[dates[0], 'AAPL'] > 0, "s_log_1p failed"
    
    # 2. Binary Operators
    print("Testing min, max, power...")
    res_min = parser.evaluate("min(close, 200)")
    assert res_min.loc[dates[0], 'MSFT'] == 200.0, "min failed"
    
    res_max = parser.evaluate("max(close, 200)")
    assert res_max.loc[dates[0], 'AAPL'] == 200.0, "max failed"
    
    res_pow = parser.evaluate("power(close, 2)")
    assert res_pow.loc[dates[0], 'AAPL'] == 22500.0, "power failed"
    
    # 3. 3-Arg Operators: clamp
    print("Testing clamp...")
    res_clamp = parser.evaluate("clamp(close, 151, 154)")
    assert res_clamp.loc[dates[0], 'AAPL'] == 151.0, "clamp min failed"
    assert res_clamp.loc[dates[2], 'AAPL'] == 154.0, "clamp max failed"
    
    # 4. 2-Arg Time Series Operators
    print("Testing ts_argmax, ts_argmin, ts_skewness, ts_kurtosis, ts_product, ts_decay_exp, ts_min_max_scale, ts_av_diff...")
    res_argmax = parser.evaluate("ts_argmax(close, 2)")
    # For AAPL: [150, 152] -> argmax day since = 0 (today is max), [152, 155] -> 0, [155, 151] -> 1 (yesterday was max)
    assert res_argmax.loc[dates[3], 'AAPL'] == 1, "ts_argmax failed"
    
    res_argmin = parser.evaluate("ts_argmin(close, 2)")
    assert res_argmin.loc[dates[3], 'AAPL'] == 0, "ts_argmin failed"
    
    res_skew = parser.evaluate("ts_skewness(close, 3)")
    assert not res_skew.loc[dates[2]].isna().any(), "ts_skewness failed"
    
    res_kurt = parser.evaluate("ts_kurtosis(close, 4)")
    assert not res_kurt.loc[dates[3]].isna().any(), "ts_kurtosis failed"
    
    res_prod = parser.evaluate("ts_product(close, 2)")
    assert res_prod.loc[dates[1], 'AAPL'] == 150.0 * 152.0, "ts_product failed"
    
    res_decay_exp = parser.evaluate("ts_decay_exp(close, 1.5)")
    assert not res_decay_exp.isna().any().any(), "ts_decay_exp failed"
    
    res_scale = parser.evaluate("ts_min_max_scale(close, 2)")
    assert res_scale.loc[dates[1], 'AAPL'] == 1.0, "ts_min_max_scale failed"
    
    res_av_diff = parser.evaluate("ts_av_diff(close, 2)")
    assert res_av_diff.loc[dates[1], 'AAPL'] == 152.0 - (150.0 + 152.0)/2, "ts_av_diff failed"
    
    # 5. Regression Operators
    print("Testing ts_regression_beta, ts_regression_alpha, ts_regression_residual...")
    res_beta = parser.evaluate("ts_regression_beta(sentiment, close, 2)")
    assert not res_beta.iloc[1:].isna().any().any(), "ts_regression_beta failed"
    
    res_alpha = parser.evaluate("ts_regression_alpha(sentiment, close, 2)")
    assert not res_alpha.iloc[1:].isna().any().any(), "ts_regression_alpha failed"
    
    res_resid = parser.evaluate("ts_regression_residual(sentiment, close, 2)")
    assert not res_resid.iloc[1:].isna().any().any(), "ts_regression_residual failed"
    
    # 6. trade_when Operator
    print("Testing trade_when...")
    # Condition: trigger if close > 151, alpha_val is sentiment, exit if close <= 150
    # AAPL close: [150 (exit), 152 (trigger), 155 (hold), 151 (hold)]
    # expected: [NaN, 0.6 (sentiment), 0.2 (sentiment_prev), -0.1 (sentiment_prev)]
    res_trade = parser.evaluate("trade_when(close > 151, sentiment, close <= 150)")
    assert pd.isna(res_trade.loc[dates[0], 'AAPL']), "trade_when failed on exit at t0"
    assert res_trade.loc[dates[1], 'AAPL'] == 0.6, "trade_when failed on trigger"
    assert res_trade.loc[dates[2], 'AAPL'] == 0.2, "trade_when failed on hold"
    
    print("[OK] All new WorldQuant Brain operators tested and passed successfully!")

if __name__ == "__main__":
    try:
        test_new_operators()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
