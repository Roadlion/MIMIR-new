# scripts/create_user.py
import sys
import os
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.auth import hash_password
from backend.app.database import get_db_connection_dict
from backend.app.config import get_settings

settings = get_settings()
schema = settings.mimir_schema

def create_user_cli(username: str, password: str, role: str = "user", quota: int = 100):
    conn = get_db_connection_dict()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM {schema}.mimir_users WHERE username = %s", (username.strip(),))
        if cur.fetchone():
            print(f"Error: User '{username}' already exists.")
            sys.exit(1)

        hashed = hash_password(password)
        cur.execute(
            f"""INSERT INTO {schema}.mimir_users (username, password_hash, role, daily_token_quota)
               VALUES (%s, %s, %s, %s) RETURNING id, username, role, daily_token_quota""",
            (username.strip(), hashed, role, quota)
        )
        new_user = cur.fetchone()
        conn.commit()
        cur.close()
        print(f"Successfully created account for '{new_user['username']}' (ID: {new_user['id']}, Role: {new_user['role']}, Daily Quota: {new_user['daily_token_quota']} msgs/day).")
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a new MIMIR user account")
    parser.add_argument("--username", "-u", required=True, help="Username for the new account")
    parser.add_argument("--password", "-p", required=True, help="Password for the new account")
    parser.add_argument("--role", "-r", default="user", choices=["user", "admin"], help="Role (user or admin)")
    parser.add_argument("--quota", "-q", type=int, default=100, help="Daily Oracle message quota")

    args = parser.parse_args()
    create_user_cli(args.username, args.password, args.role, args.quota)
