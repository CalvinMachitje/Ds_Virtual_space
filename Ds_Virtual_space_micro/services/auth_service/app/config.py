# services/auth_service/app/config.py
ALLOWED_REDIRECT_DOMAINS = [
    "localhost:5173",
    # Add production domain later, e.g.:
    # "yourdomain.com",
    # "www.yourdomain.com"
]

# You can add more shared constants here in the future
DEFAULT_PAGE_SIZE = 9
MAX_PASSWORD_RESET_ATTEMPTS = 5
RATE_LIMIT_DEFAULT = "200 per day; 50 per hour"