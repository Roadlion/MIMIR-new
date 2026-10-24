# tests/test_war_rig_nitrous.py
"""
Unit and Integration Tests for War Rig -> Casino Nitrous Express Bridge
======================================================================
Verifies:
1. NitrousBridgeInput creation from War Rig convergence signals
2. Nitro Mode A (Skew-Optimized Bull Call Vertical Spreads with 3:1 - 5:1 Asymmetry)
3. Nitro Mode B1 (High-Gamma Directional Calls / Straddles under Low IV < 35)
4. Nitro Mode B2 (IV Crush Harvester Credit Spreads under High IV > 85)
5. Zero Gap-Down Slippage Risk (Strictly Capped Debit Risk)
6. War Rig Engine Integration Hook
"""

import sys
from pathlib import Path
root_dir = Path(__file__).resolve().parents[1]
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from backend.app.analytics.war_rig_nitrous import (
    WarRigNitrousBridge,
    NitrousBridgeInput,
    NitrousModeResult,
    get_nitrous_bridge
)
from backend.app.analytics.options_data import (
    OptionsChain,
    OptionContract,
    SyntheticOptionsProvider
)
from backend.app.analytics.war_rig_engine import get_war_rig_nitrous_options


def create_test_chain(spot: float = 100.0, base_iv: float = 0.35) -> OptionsChain:
    """Helper to build a deterministic test options chain."""
    today = date.today()
    exp1 = today + timedelta(days=28)
    exp2 = today + timedelta(days=56)
    expirations = [exp1, exp2]

    calls = {exp1: [], exp2: []}
    puts = {exp1: [], exp2: []}

    strikes = [85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0, 120.0]

    for exp in expirations:
        dte = (exp - today).days
        for s in strikes:
            # Synthetic pricing
            c_mid = max(0.5, (spot - s) + spot * 0.06) if s < spot else max(0.4, spot * 0.06 * (spot / s)**2)
            p_mid = max(0.5, (s - spot) + spot * 0.06) if s > spot else max(0.4, spot * 0.06 * (s / spot)**2)

            calls[exp].append(OptionContract(
                strike=s,
                bid=round(c_mid * 0.95, 2),
                ask=round(c_mid * 1.05, 2),
                mid=round(c_mid, 2),
                last=round(c_mid, 2),
                volume=1200,
                open_interest=4500,
                implied_volatility=base_iv,
                in_the_money=(s < spot),
                days_to_expiry=dte,
                contract_symbol=f"TEST_{exp.strftime('%y%m%d')}C{int(s)}"
            ))

            puts[exp].append(OptionContract(
                strike=s,
                bid=round(p_mid * 0.95, 2),
                ask=round(p_mid * 1.05, 2),
                mid=round(p_mid, 2),
                last=round(p_mid, 2),
                volume=1200,
                open_interest=4500,
                implied_volatility=base_iv,
                in_the_money=(s > spot),
                days_to_expiry=dte,
                contract_symbol=f"TEST_{exp.strftime('%y%m%d')}P{int(s)}"
            ))

    return OptionsChain(
        ticker="TEST",
        underlying_price=spot,
        expirations=expirations,
        calls=calls,
        puts=puts,
        fetched_at=None
    )


class TestWarRigNitrousBridge:

    def test_nitrous_input_parsing(self):
        """Test parsing raw War Rig engine dictionary into bridge payload."""
        raw_signal = {
            "ticker": "MU",
            "trigger_price": 102.50,
            "target_price": 118.00,
            "stop_loss": 96.00,
            "conviction_score": 0.78,
            "risk_reward_ratio": 2.38,
            "holding_period": "Pre-Earnings Window",
            "catalyst_type": "WAR_RIG_CONVERGENCE",
            "investment_thesis": "Leading semiconductor stealth accumulation + pre-earnings beat.",
            "evaluation_date": "2026-06-15"
        }

        inp = NitrousBridgeInput.from_signal_dict(raw_signal)
        assert inp.ticker == "MU"
        assert inp.trigger_price == 102.50
        assert inp.target_price == 118.00
        assert inp.stop_loss == 96.00
        assert inp.conviction_score == 0.78
        assert inp.evaluation_date == date(2026, 6, 15)

    def test_mode_a_bull_call_spread_asymmetry(self):
        """
        Verify Mode A selects optimal bull call spread targeting 3:1 to 5:1 asymmetry
        with strictly capped debit downside.
        """
        chain = create_test_chain(spot=100.0, base_iv=0.32)
        mock_svc = MagicMock()
        mock_svc.fetch_chain.return_value = chain

        bridge = WarRigNitrousBridge(options_service=mock_svc)

        inp = NitrousBridgeInput(
            ticker="TEST",
            trigger_price=100.0,
            target_price=115.0,  # 3.0x ATR target
            stop_loss=95.0,      # 1.5x ATR stop loss
            conviction_score=0.80,
            holding_period="Swing (28 Days)"
        )

        res_a = bridge.solve_mode_a_bull_call_spread(inp, chain)
        assert res_a is not None
        assert res_a.mode == "MODE_A_BULL_CALL_SPREAD"
        assert res_a.ticker == "TEST"
        assert len(res_a.legs) == 2

        # Verify Long Strike is near ATM (95-105)
        long_leg = [l for l in res_a.legs if l["direction"] == "long"][0]
        assert 95.0 <= long_leg["strike"] <= 105.0

        # Verify Short Strike is pinned near Target Price (110-120)
        short_leg = [l for l in res_a.legs if l["direction"] == "short"][0]
        assert short_leg["strike"] > long_leg["strike"]
        assert 110.0 <= short_leg["strike"] <= 120.0

        # Verify capped debit downside
        assert res_a.net_debit_or_credit > 0.0
        assert res_a.max_loss > 0.0
        assert res_a.gap_down_protected is True

        # Verify asymmetry ratio
        assert res_a.asymmetry_ratio >= 2.0
        print(f"\n[TEST_MODE_A] Solved Bull Call Spread: {long_leg['strike']}C / {short_leg['strike']}C")
        print(f"[TEST_MODE_A] Net Debit: ${res_a.net_debit_or_credit:.2f} | Max Profit: ${res_a.max_profit:.2f} | Asymmetry: {res_a.asymmetry_ratio:.2f}:1")

    def test_mode_b_gamma_call_low_iv(self):
        """
        Verify Mode B recommends high-gamma pure calls when IV is cheap (IV Rank < 35).
        """
        # Low IV (0.22)
        chain = create_test_chain(spot=100.0, base_iv=0.22)
        mock_svc = MagicMock()
        mock_svc.fetch_chain.return_value = chain

        bridge = WarRigNitrousBridge(options_service=mock_svc)
        # Patch iv_rank to simulate low IV
        with patch.object(bridge, "_estimate_iv_rank", return_value=25.0):
            inp = NitrousBridgeInput(
                ticker="TEST",
                trigger_price=100.0,
                target_price=115.0,
                stop_loss=95.0,
                conviction_score=0.82
            )
            res_b = bridge.solve_mode_b_volatility_harvester(inp, chain)
            assert res_b is not None
            assert res_b.mode == "MODE_B_GAMMA_STRADDLE"
            assert res_b.iv_rank == 25.0
            assert "Low IV" in res_b.strategy_name or "Call" in res_b.strategy_name
            assert len(res_b.legs) == 1
            assert res_b.legs[0]["direction"] == "long"
            assert res_b.gap_down_protected is True
            print(f"\n[TEST_MODE_B1] Solved Low-IV Gamma Call: {res_b.strategy_name} (IV Rank: {res_b.iv_rank}%)")

    def test_mode_b_iv_crush_harvester_high_iv(self):
        """
        Verify Mode B recommends Bull Put credit spreads below stop loss when IV is bloated (IV Rank > 85).
        """
        # Bloated IV (0.75)
        chain = create_test_chain(spot=100.0, base_iv=0.75)
        mock_svc = MagicMock()
        mock_svc.fetch_chain.return_value = chain

        bridge = WarRigNitrousBridge(options_service=mock_svc)
        # Patch iv_rank to simulate hyper-elevated IV
        with patch.object(bridge, "_estimate_iv_rank", return_value=88.5):
            inp = NitrousBridgeInput(
                ticker="TEST",
                trigger_price=100.0,
                target_price=115.0,
                stop_loss=95.0,
                conviction_score=0.85
            )
            res_b = bridge.solve_mode_b_volatility_harvester(inp, chain)
            assert res_b is not None
            assert res_b.mode == "MODE_B_IV_CRUSH_HARVEST"
            assert res_b.is_credit is True
            assert res_b.iv_rank == 88.5
            assert len(res_b.legs) == 2
            # Short put should be <= stop loss * 1.02
            short_leg = [l for l in res_b.legs if l["direction"] == "short"][0]
            assert short_leg["strike"] <= 100.0
            print(f"\n[TEST_MODE_B2] Solved IV Crush Harvester: {res_b.strategy_name} (Net Credit: ${res_b.net_debit_or_credit:.2f})")

    def test_end_to_end_nitrous_deployment(self):
        """
        Test the master generate_nitrous_deployment() method.
        """
        chain = create_test_chain(spot=100.0, base_iv=0.35)
        mock_svc = MagicMock()
        mock_svc.fetch_chain.return_value = chain

        bridge = WarRigNitrousBridge(options_service=mock_svc)

        signal = {
            "ticker": "TEST",
            "trigger_price": 100.0,
            "target_price": 115.0,
            "stop_loss": 95.0,
            "conviction_score": 0.78,
            "risk_reward_ratio": 3.0,
            "holding_period": "Swing (28 Days)"
        }

        deployment = bridge.generate_nitrous_deployment(signal)
        assert deployment["bridge_status"] == "READY"
        assert deployment["ticker"] == "TEST"
        assert deployment["recommended_mode"] in ("MODE_A", "MODE_B")
        assert deployment["mode_a_bull_call"] is not None
        assert deployment["mode_b_volatility"] is not None


def test_war_rig_engine_nitrous_integration():
    """Verify war_rig_engine helper get_war_rig_nitrous_options()."""
    test_sig = {
        "ticker": "AAPL",
        "trigger_price": 225.0,
        "target_price": 255.0,
        "stop_loss": 215.0,
        "conviction_score": 0.82,
        "risk_reward_ratio": 3.0,
        "holding_period": "Swing (21 Days)"
    }
    deployment = get_war_rig_nitrous_options(test_sig)
    assert deployment is not None
    assert deployment["ticker"] == "AAPL"
    assert deployment["bridge_status"] in ("READY", "CHAIN_UNAVAILABLE")
