# scripts/test_multi_user.py
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)

def run_tests():
    print("==================================================")
    print("  RUNNING MULTI-USER & AUTHENTICATION TESTS")
    print("==================================================")

    # 1. Login as Admin
    print("\n[Test 1] Logging in as Admin...")
    res = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    assert res.status_code == 200, f"Admin login failed: {res.text}"
    admin_token = res.json()["token"]
    print("[OK] Admin login successful! Token retrieved.")

    # 2. Check /auth/me for Admin
    print("\n[Test 2] Checking Admin Profile (/auth/me)...")
    res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    profile = res.json()
    assert profile["username"] == "admin"
    assert profile["role"] == "admin"
    print("[OK] Admin profile verified.")

    # 3. Create a test friend account via Admin API
    print("\n[Test 3] Admin creating friend account 'bob'...")
    res = client.post(
        "/api/v1/admin/users",
        json={"username": "bob", "password": "password123", "role": "user", "daily_token_quota": 100},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    if res.status_code == 400 and "already exists" in res.text:
        print("[OK] Account 'bob' already exists.")
    else:
        assert res.status_code == 200, f"Create user failed: {res.text}"
        print("[OK] Account 'bob' created successfully.")

    # 4. Login as 'bob'
    print("\n[Test 4] Logging in as 'bob'...")
    res = client.post("/api/v1/auth/login", json={"username": "bob", "password": "password123"})
    assert res.status_code == 200, f"Bob login failed: {res.text}"
    bob_token = res.json()["token"]
    print("[OK] Bob login successful! Token retrieved.")

    # 5. Check Bob profile & initial daily quota
    print("\n[Test 5] Checking Bob Profile & Quota...")
    res = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bob_token}"})
    assert res.status_code == 200
    bob_profile = res.json()
    assert bob_profile["username"] == "bob"
    assert bob_profile["daily_token_quota"] == 100
    assert bob_profile["remaining_quota"] <= 100
    print(f"[OK] Bob profile verified (Remaining quota: {bob_profile['remaining_quota']}/100 msgs).")

    # 6. Test Data Isolation: Chat Sessions
    print("\n[Test 6] Testing Chat Session Data Isolation...")
    # Bob creates a chat session
    res = client.post(
        "/api/v1/research/sessions",
        json={"title": "Bob Secret Research"},
        headers={"Authorization": f"Bearer {bob_token}"}
    )
    assert res.status_code == 200, f"Bob session creation failed: {res.text}"
    bob_session_id = res.json()["id"]

    # Bob lists sessions (should include Bob Secret Research)
    res = client.get("/api/v1/research/sessions", headers={"Authorization": f"Bearer {bob_token}"})
    bob_sessions = [s["id"] for s in res.json()["sessions"]]
    assert bob_session_id in bob_sessions

    # Admin lists sessions (should NOT include Bob Secret Research)
    res = client.get("/api/v1/research/sessions", headers={"Authorization": f"Bearer {admin_token}"})
    admin_sessions = [s["id"] for s in res.json()["sessions"]]
    assert bob_session_id not in admin_sessions
    print("[OK] Chat sessions are strictly isolated per user!")

    # 7. Test Admin User List API
    print("\n[Test 7] Testing Admin User Management List API...")
    res = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    users_list = res.json()["users"]
    usernames = [u["username"] for u in users_list]
    assert "admin" in usernames
    assert "bob" in usernames
    print(f"[OK] Admin listed all {len(users_list)} registered accounts!")

    # 8. Test Data Isolation: Portfolio Overview
    print("\n[Test 8] Testing Portfolio Data Isolation...")
    # Admin gets portfolio
    admin_port = client.get("/api/v1/portfolio", headers={"Authorization": f"Bearer {admin_token}"}).json()
    # Bob gets portfolio
    bob_port = client.get("/api/v1/portfolio", headers={"Authorization": f"Bearer {bob_token}"}).json()
    
    assert bob_port["holdings"] == {}, "Bob portfolio should be blank for a new user account!"
    print("[OK] Portfolio is strictly isolated! Bob sees a blank portfolio.")

    print("\n==================================================")
    print("  ALL MULTI-USER VERIFICATION TESTS PASSED!")
    print("==================================================")

if __name__ == "__main__":
    run_tests()
