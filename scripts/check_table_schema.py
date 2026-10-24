# scripts/check_table_schema.py
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()
DB_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()
cur.execute("""
    SELECT column_name, data_type 
    FROM information_schema.columns 
    WHERE table_schema = 'yggdrasil' AND table_name = 'mimir_asset_relationships'
""")
cols = cur.fetchall()
print("Columns in mimir_asset_relationships:")
for c, t in cols:
    print(f" - {c}: {t}")
cur.close()
conn.close()
