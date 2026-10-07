# backend/app/database.py
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from .config import get_settings

settings = get_settings()

_pool = None
_dict_pool = None


class _PoolProxy:
    """Wraps a psycopg2 connection so .close() returns it to the pool instead of destroying it."""

    def __init__(self, conn, pool):
        self.__dict__['_conn'] = conn
        self.__dict__['_pool'] = pool
        self.__dict__['_is_closed'] = False

    def __getattr__(self, name):
        conn = self.__dict__.get('_conn')
        if not conn or self.__dict__.get('_is_closed'):
            raise psycopg2.InterfaceError("connection already closed")
        return getattr(conn, name)

    def __setattr__(self, name, value):
        if name in ('_conn', '_pool', '_is_closed'):
            self.__dict__[name] = value
        else:
            conn = self.__dict__.get('_conn')
            if not conn or self.__dict__.get('_is_closed'):
                raise psycopg2.InterfaceError("connection already closed")
            setattr(conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        # Idempotent: ensure close() can only ever run once per checkout
        if self.__dict__.get('_is_closed'):
            return
        self.__dict__['_is_closed'] = True
        conn = self.__dict__.get('_conn')
        pool = self.__dict__.get('_pool')
        if conn is not None and pool is not None:
            try:
                if not conn.closed:
                    pool.putconn(conn)
                else:
                    pool.putconn(conn, close=True)
            except Exception:
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass


def _get_pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            minconn=4,
            maxconn=60,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            options="-c zero_damaged_pages=on",
        )
    return _pool


def _get_dict_pool():
    global _dict_pool
    if _dict_pool is None:
        _dict_pool = ThreadedConnectionPool(
            minconn=4,
            maxconn=60,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            cursor_factory=RealDictCursor,
            options="-c zero_damaged_pages=on",
        )
    return _dict_pool


def get_db_connection():
    """Get a healthy connection from the pool. Call conn.close() or use `with get_db_connection() as conn:` to return it."""
    pool = _get_pool()
    conn = pool.getconn()
    while conn.closed:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = pool.getconn()
    return _PoolProxy(conn, pool)


def get_db_connection_dict():
    """Get a healthy RealDictCursor connection from the pool. Call conn.close() or use `with get_db_connection_dict() as conn:` to return it."""
    pool = _get_dict_pool()
    conn = pool.getconn()
    while conn.closed:
        try:
            pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = pool.getconn()
    return _PoolProxy(conn, pool)

