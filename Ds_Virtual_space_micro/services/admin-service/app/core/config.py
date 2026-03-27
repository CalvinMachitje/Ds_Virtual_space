# Configuration settings for the Admin Service using Pydantic's BaseSettings.
# services/admin-service/app/core/config.py
from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    # Supabase
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # JWT
    JWT_SECRET_KEY: str
    JWT_ACCESS_TOKEN_EXPIRES_MINUTES: int = 10080   # 7 days
    JWT_REFRESH_TOKEN_EXPIRES_DAYS: int = 30

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Rate limits (match your auth-service)
    RATE_LIMIT_ADMIN_LOGIN: str = "5 per minute"
    RATE_LIMIT_GENERAL: str = "100 per minute"

    # Frontend origins for CORS
    FRONTEND_ORIGINS: List[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    class Config:
        env_file = ".env"
        extra = "ignore"

settings = Settings()