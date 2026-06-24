# backend/app/config.py
from pydantic_settings import BaseSettings
from functools import lru_cache
from pydantic import Field

class Settings(BaseSettings):
    # DB
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "pantheon_db"
    db_user: str = "postgres"
    db_password: str = ""
    
    # MIMIR Schema
    mimir_schema: str = "yggdrasil"
    
    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    
    # Redis (later)
    redis_url: str = "redis://localhost:6379/0"
    
    # News API keys
    newsapi_key: str = ""
    gnews_api_key: str = ""
    
    # Mode - map from MIMIR_MODE env var
    mode: str = Field(default="standalone", alias="MIMIR_MODE")
    
    class Config:
        env_file = ".env"
        extra = "ignore"  # <-- IGNORE extra fields instead of failing

@lru_cache()
def get_settings():
    return Settings()