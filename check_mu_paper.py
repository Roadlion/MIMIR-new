import sys
sys.path.append('e:/lion_stuff/Da Projects/MIMIR-new')
from backend.app.config import get_settings
from backend.app.database import get_db_connection_dict

settings = get_settings()
conn = get_db_connection_dict()
cur = conn.cursor()
cur.execute(f"SELECT ticker, transaction_type, quantity, order_date FROM {settings.mimir_schema}.mimir_paper_portfolio WHERE ticker='MU'")
print(cur.fetchall())
cur.close()
conn.close()
