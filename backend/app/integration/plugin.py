# backend/app/integration/plugin.py
from fastapi import APIRouter
from app.routers import articles
from app.config import get_settings

class MIMIRPlugin:
    """MIMIR plugin interface for ASGARD."""
    
    name = "mimir"
    version = "1.0.0"
    description = "Market Intelligence & Macroeconomic Indicator Reactor"
    
    @classmethod
    def mount_routes(cls, router: APIRouter, prefix: str = "/mimir"):
        """Mount MIMIR routes onto ASGARD's router."""
        router.include_router(articles.router, prefix=prefix, tags=["mimir"])
        return router
    
    @classmethod
    def get_schemas(cls):
        return ["asgard"]
    
    @classmethod
    def get_tables(cls):
        return [
            "asgard.mimir_raw_articles",
            "asgard.sentiment_snapshot",
            "asgard.asset_master",
            "asgard.signal_log"
        ]
    
    @classmethod
    def on_load(cls):
        settings = get_settings()
        print(f"[MIMIR] Loaded as plugin v{cls.version} in {settings.mode} mode")
        return True