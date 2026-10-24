# scripts/migrate_multi_user.py
import sys
import os
import hashlib
import secrets

# Add project root to python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.app.database import get_db_connection
from backend.app.config import get_settings

settings = get_settings()
schema = settings.mimir_schema

def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with salt."""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    )
    return f"{salt}${key.hex()}"

def run_migration():
    conn = get_db_connection()
    cur = conn.cursor()

    try:
        print(f"Starting multi-user migration on schema '{schema}'...")
        
        # 1. Create mimir_users table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_users (
                id SERIAL PRIMARY KEY,
                username VARCHAR(64) UNIQUE NOT NULL,
                password_hash VARCHAR(256) NOT NULL,
                role VARCHAR(32) NOT NULL DEFAULT 'user',
                daily_token_quota INT NOT NULL DEFAULT 100,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        print("Table 'mimir_users' ensured.")

        # 2. Seed initial admin user if not exists
        cur.execute(f"SELECT id FROM {schema}.mimir_users WHERE username = 'admin'")
        admin_row = cur.fetchone()
        if not admin_row:
            # Default admin password is 'admin123'
            default_admin_pass = "admin123"
            hashed = hash_password(default_admin_pass)
            cur.execute(
                f"INSERT INTO {schema}.mimir_users (username, password_hash, role, daily_token_quota) VALUES (%s, %s, 'admin', 99999) RETURNING id",
                ('admin', hashed)
            )
            admin_id = cur.fetchone()[0]
            print(f"Created default admin account (username: 'admin', pass: '{default_admin_pass}', user_id: {admin_id}).")
        else:
            admin_id = admin_row[0]
            print(f"Admin account exists with user_id: {admin_id}.")

        # 3. Create mimir_daily_recaps table for caching news summaries
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_daily_recaps (
                id SERIAL PRIMARY KEY,
                recap_date DATE UNIQUE NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        print("Table 'mimir_daily_recaps' ensured.")

        # 4. Create mimir_oracle_daily_usage table
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {schema}.mimir_oracle_daily_usage (
                user_id INT REFERENCES {schema}.mimir_users(id) ON DELETE CASCADE,
                usage_date DATE NOT NULL,
                message_count INT NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            );
        """)
        print("Table 'mimir_oracle_daily_usage' ensured.")

        # 5. Add user_id column to existing personal tables
        target_tables = [
            "mimir_portfolio",
            "mimir_chat_sessions",
            "mimir_paper_trading_config",
            "mimir_paper_trade_log",
            "mimir_paper_portfolio",
            "mimir_casino_strategies",
            "mimir_casino_positions",
            "mimir_api_cost_ledger"
        ]

        for tbl in target_tables:
            # Check if table exists
            cur.execute(f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_schema = %s AND table_name = %s
                );
            """, (schema, tbl))
            exists = cur.fetchone()[0]
            
            if exists:
                # Add user_id column if missing
                cur.execute(f"""
                    ALTER TABLE {schema}.{tbl} 
                    ADD COLUMN IF NOT EXISTS user_id INT REFERENCES {schema}.mimir_users(id) DEFAULT 1;
                """)
                # Backfill nulls if any
                cur.execute(f"UPDATE {schema}.{tbl} SET user_id = %s WHERE user_id IS NULL", (admin_id,))
                print(f"Updated table '{tbl}' with user_id foreign key.")

        conn.commit()
        print("Multi-user database migration completed successfully!")

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        raise e
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    run_migration()
