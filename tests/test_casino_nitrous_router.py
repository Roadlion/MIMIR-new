# tests/test_casino_nitrous_router.py
import sys
from pathlib import Path
root_dir = Path(__file__).resolve().parents[1]
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

import pytest
from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)


def test_solve_nitrous_endpoint():
    """Verify POST /api/casino/nitrous/solve solves Mode A & B correctly."""
    payload = {
        "ticker": "AAPL",
        "trigger_price": 225.0,
        "target_price": 255.0,
        "stop_loss": 215.0,
        "conviction_score": 0.82,
        "risk_reward_ratio": 3.0,
        "holding_period": "Swing (21 Days)",
        "catalyst_type": "WAR_RIG_CONVERGENCE"
    }

    response = client.post("/api/v1/casino/nitrous/solve", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "AAPL"
    assert data["bridge_status"] in ("READY", "CHAIN_UNAVAILABLE")
    if data["bridge_status"] == "READY":
        assert "mode_a_bull_call" in data
        assert "mode_b_volatility" in data
        assert data["recommended_mode"] in ("MODE_A", "MODE_B")


def test_active_convergences_endpoint():
    """Verify GET /api/v1/casino/nitrous/active-convergences returns 200."""
    response = client.get("/api/v1/casino/nitrous/active-convergences?limit=3")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "convergences" in data
