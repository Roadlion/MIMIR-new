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

    def __getattr__(self, name):
        return getattr(self.__dict__['_conn'], name)

    def __setattr__(self, name, value):
        setattr(self.__dict__['_conn'], name, value)

    def close(self):
        try:
            self.__dict__['_pool'].putconn(self.__dict__['_conn'])
        except Exception:
            self.__dict__['_conn'].close()


def _get_pool():
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
        )
    return _pool


def _get_dict_pool():
    global _dict_pool
    if _dict_pool is None:
        _dict_pool = ThreadedConnectionPool(
            minconn=2,
            maxconn=20,
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=settings.db_user,
            password=settings.db_password,
            cursor_factory=RealDictCursor,
        )
    return _dict_pool


def get_db_connection():
    """Get a connection from the pool. Call conn.close() to return it."""
    pool = _get_pool()
    return _PoolProxy(pool.getconn(), pool)


def get_db_connection_dict():
    """Get a RealDictCursor connection from the pool. Call conn.close() to return it."""
    pool = _get_dict_pool()
    return _PoolProxy(pool.getconn(), pool)
