import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.app.services.discord_notifier import (
    _resolve_copilot_webhook,
    send_copilot_buy_alert,
    send_copilot_ratchet_alert,
    send_copilot_exit_alert,
    get_webhook_url
)

def test_webhook_resolution():
    admin_webhook = get_webhook_url("trades")
    assert admin_webhook, "Admin webhook should be configured in settings"

    print("=" * 60)
    print("TEST 1: Webhook Resolution Logic")
    print("=" * 60)

    # 1. Admin user without dedicated webhook -> should resolve to admin_webhook
    admin_user = {"id": 1, "username": "admin", "discord_webhook_trades": None}
    res = _resolve_copilot_webhook(admin_user)
    assert res == admin_webhook, f"Admin fallback failed: got {res}"
    print("[PASS] Admin with NULL webhook resolves to admin trades webhook.")

    # 2. Non-admin user without dedicated webhook -> MUST return None
    alex_user = {"id": 2, "username": "alex", "discord_webhook_trades": None}
    res = _resolve_copilot_webhook(alex_user)
    assert res is None, f"Non-admin with NULL webhook leaked: got {res}"
    print("[PASS] Non-admin 'alex' with NULL webhook resolves to None (suppressed).")

    # 3. Non-admin user with webhook explicitly pointing to admin webhook -> MUST return None
    malicious_user = {"id": 3, "username": "bob", "discord_webhook_trades": admin_webhook}
    res = _resolve_copilot_webhook(malicious_user)
    assert res is None, f"Non-admin targeting admin webhook leaked: got {res}"
    print("[PASS] Non-admin 'bob' pointing to admin webhook is blocked/suppressed.")

    # 4. Non-admin user with their own dedicated webhook -> should resolve to their own webhook
    custom_url = "https://discord.com/api/webhooks/999999999999999999/valid_user_token"
    legit_user = {"id": 37, "username": "zeroni02", "discord_webhook_trades": custom_url}
    res = _resolve_copilot_webhook(legit_user)
    assert res == custom_url, f"Non-admin custom webhook failed: got {res}"
    print("[PASS] Non-admin 'zeroni02' with dedicated webhook resolves to dedicated URL.")


def test_copilot_alerts_isolation():
    print("\n" + "=" * 60)
    print("TEST 2: Co-Pilot Alert Dispatch Isolation")
    print("=" * 60)

    admin_webhook = get_webhook_url("trades")
    admin_user = {"id": 1, "username": "admin", "discord_id": "123456", "discord_webhook_trades": admin_webhook}
    non_admin_user = {"id": 2, "username": "alex", "discord_id": "789012", "discord_webhook_trades": None}

    eval_result = {
        "ticker": "LMT",
        "shares": 10,
        "entry_price": 450.0,
        "stop_loss": 435.0,
        "target_price": 480.0,
        "estimated_cost": 4500.0,
        "risk_dollars": 150.0,
        "risk_pct_actual": 0.9,
        "position_pct": 9.0,
        "sector": "Aerospace & Defense",
        "current_sector_pct": 0.0,
        "new_sector_pct": 9.0,
        "dry_powder_before": 50000.0,
        "dry_powder_after": 45500.0,
        "holding_period_days": 10,
        "catalyst_type": "WAR_RIG_CONVERGENCE"
    }

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_post.return_value = mock_resp

        # 1. Test Buy Alert for non-admin
        sent = send_copilot_buy_alert(non_admin_user, eval_result)
        assert sent is False, "Non-admin buy alert should return False"
        assert mock_post.call_count == 0, f"Expected 0 calls, but got {mock_post.call_count}"
        print("[PASS] send_copilot_buy_alert for non-admin returned False, 0 HTTP requests sent.")

        # 2. Test Ratchet Alert for non-admin
        sent = send_copilot_ratchet_alert(
            user=non_admin_user,
            ticker="LMT",
            shares=10,
            old_sl=435.0,
            new_sl=455.0,
            entry_price=450.0,
            current_price=460.0,
            locked_profit=50.0,
            gain_pct=2.22
        )
        assert sent is False, "Non-admin ratchet alert should return False"
        assert mock_post.call_count == 0, f"Expected 0 calls, but got {mock_post.call_count}"
        print("[PASS] send_copilot_ratchet_alert for non-admin returned False, 0 HTTP requests sent.")

        # 3. Test Exit Alert for non-admin
        sent = send_copilot_exit_alert(
            user=non_admin_user,
            ticker="LMT",
            shares=10,
            entry_price=450.0,
            exit_price=465.0,
            realized_pnl=150.0,
            return_pct=3.33,
            cash_recovered=4650.0,
            new_dry_powder=50000.0,
            exit_reason="PROFIT_TARGET"
        )
        assert sent is False, "Non-admin exit alert should return False"
        assert mock_post.call_count == 0, f"Expected 0 calls, but got {mock_post.call_count}"
        print("[PASS] send_copilot_exit_alert for non-admin returned False, 0 HTTP requests sent.")

        # 4. Test Buy Alert for Admin -> MUST succeed and call admin webhook
        sent = send_copilot_buy_alert(admin_user, eval_result)
        assert sent is True, "Admin buy alert should return True"
        assert mock_post.call_count == 1, f"Expected 1 call for admin, got {mock_post.call_count}"
        called_url = mock_post.call_args[0][0]
        assert called_url == admin_webhook, f"Admin alert went to wrong URL: {called_url}"
        print(f"[PASS] send_copilot_buy_alert for admin succeeded, sent to admin webhook.")


if __name__ == "__main__":
    test_webhook_resolution()
    test_copilot_alerts_isolation()
    print("\n" + "=" * 60)
    print("ALL DISCORD ISOLATION TESTS PASSED (100% OK)")
    print("=" * 60)
