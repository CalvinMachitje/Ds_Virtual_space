# services/api-gateway/app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List
import os

class Settings(BaseSettings):
    # Required
    JWT_SECRET_KEY: str

    # Optional with defaults
    REDIS_URL: str = "redis://localhost:6379/0"
    FRONTEND_ORIGINS: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    model_config = SettingsConfigDict(
        env_file=".env",          
        env_file_encoding="utf-8",
        extra="ignore",            
        case_sensitive=False
    )

# Instantiate once
settings = Settings()