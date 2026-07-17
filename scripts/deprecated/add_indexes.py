import os
from backend.app.database import get_db_connection

def add_indexes():
    conn = get_db_connection()
    cur = conn.cursor()
    queries = [
        "CREATE INDEX IF NOT EXISTS idx_mimir_articles_pub_ts ON yggdrasil.mimir_raw_articles(published_ts);",
        "CREATE INDEX IF NOT EXISTS idx_mimir_impacts_article_id ON yggdrasil.mimir_sentiment_impacts(article_id);",
        "CREATE INDEX IF NOT EXISTS idx_mimir_impacts_country ON yggdrasil.mimir_sentiment_impacts(country);",
        "CREATE INDEX IF NOT EXISTS idx_mimir_impacts_region ON yggdrasil.mimir_sentiment_impacts(region);"
    ]
    for q in queries:
        print(f"Executing: {q}")
        cur.execute(q)
    conn.commit()
    cur.close()
    conn.close()
    print("Indexes created successfully!")

if __name__ == '__main__':
    add_indexes()
