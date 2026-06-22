# backend/app/database.py
import psycopg2
from psycopg2.extras import RealDictCursor
from .config import get_settings  # <-- FIXED: relative import

settings = get_settings()

def get_db_connection():
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password
    )

def get_db_connection_dict():
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        cursor_factory=RealDictCursor
    )