# services/auth-service/app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        # env_prefix="VITE_"  ← REMOVED THIS LINE → now reads exact variable names from .env
    )

    # ── JWT ────────────────────────────────────────────────
    JWT_SECRET_KEY: str
    JWT_ACCESS_TOKEN_EXPIRES_MINUTES: int = 10080
    JWT_REFRESH_TOKEN_EXPIRES_DAYS: int = 30

    # ── Redis ──────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── CORS allowed origins ───────────────────────────────
    FRONTEND_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://196.253.26.122:5173",
        "http://196.253.26.113:5173",
    ]

    # ── Supabase ───────────────────────────────────────────
    SUPABASE_URL: str
    SUPABASE_SERVICE_ROLE_KEY: str

    # ── Rate limits (copied from old constants.py) ────────
    RATE_LIMIT_LOGIN: str = "5 per minute; 20 per hour"
    RATE_LIMIT_ADMIN_LOGIN: str = "3 per minute; 10 per hour"
    RATE_LIMIT_SIGNUP: str = "3 per minute"
    RATE_LIMIT_REFRESH: str = "10 per minute"

    # ── Lockout thresholds ─────────────────────────────────
    ADMIN_LOCKOUT_MINUTES: int = 30
    ADMIN_FAIL_THRESHOLD: int = 5
    USER_LOCKOUT_MINUTES: int = 60
    USER_FAIL_THRESHOLD: int = 10

    # ── Allowed roles ──────────────────────────────────────
    ROLES: List[str] = ["buyer", "seller"]


settings = Settings()


# ──────────────────────────────────────────────
# TEMPORARY STARTUP DEBUG – you can remove this block later
# ──────────────────────────────────────────────
print("\n" + "=" * 60)
print("DEBUG: Loaded environment variables from .env")
print(f"  SUPABASE_URL:          {settings.SUPABASE_URL}")
print(f"  SUPABASE_SERVICE_ROLE_KEY set?  {'yes' if settings.SUPABASE_SERVICE_ROLE_KEY else 'MISSING'}")
print(f"  JWT_SECRET_KEY set?            {'yes' if settings.JWT_SECRET_KEY else 'MISSING'}")
print(f"  REDIS_URL:                     {settings.REDIS_URL}")
print("=" * 60 + "\n")