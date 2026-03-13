# services/auth-service/app/routes/auth/constants.py
# Rate limits
RATE_LIMIT_LOGIN = "5 per minute; 20 per hour"
RATE_LIMIT_ADMIN_LOGIN = "3 per minute; 10 per hour"
RATE_LIMIT_SIGNUP = "3 per minute"
RATE_LIMIT_REFRESH = "10 per minute"

# Lockout thresholds
ADMIN_LOCKOUT_MINUTES = 30
ADMIN_FAIL_THRESHOLD = 5
USER_LOCKOUT_MINUTES = 60
USER_FAIL_THRESHOLD = 10

# Roles
ROLES = ["buyer", "seller"]