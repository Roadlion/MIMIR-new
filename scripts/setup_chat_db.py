import sys
import os
from pathlib import Path
import psycopg2

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

# Load .env
dotenv_path = Path(__file__).parent.parent / '.env'
load_dotenv(dotenv_path)

DB_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS yggdrasil.mimir_chat_sessions (
    id VARCHAR(50) PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS yggdrasil.mimir_chat_messages (
    id SERIAL PRIMARY KEY,
    session_id VARCHAR(50) REFERENCES yggdrasil.mimir_chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON yggdrasil.mimir_chat_messages (session_id);
"""

def setup_db():
    print(f"Connecting to {os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")
    try:
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        cur.execute(CREATE_TABLES_SQL)
        conn.commit()
        print("Successfully created chat session tables in yggdrasil schema.")
    except Exception as e:
        print(f"Failed to create tables: {e}")
    finally:
        if 'cur' in locals():
            cur.close()
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    setup_db()
