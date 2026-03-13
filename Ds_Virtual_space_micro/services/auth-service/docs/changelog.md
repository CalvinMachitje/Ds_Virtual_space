# Changelog

All notable changes to the `auth-service` microservice will be documented here.  
Dates follow **YYYY-MM-DD** format.

---

## [2026-03-13] - Initial Full Refactor & Auth-Service Setup

### Added
- Modular Flask app factory (`app/__init__.py`) with:
  - JWT configuration and Redis-based token blocklist.
  - Health check endpoint (`/api/health`).
  - Global error handler for proper logging.
- Centralized extensions (`app/extensions.py`) for:
  - JWT, Redis, Socket.IO, Flask-Limiter, Flask-CORS, Flask-Mail, Flask-Cache, Compress, Talisman, Migrate.
  - Redis initialization with retries, logging, and safe call helper.
  - Redis pub/sub listener for live logs via Socket.IO.
- Blueprints under `app/routes/auth`:
  - `routes.py` - Core auth routes.
  - `oauth.py` - OAuth handling.
  - `twofa.py` - Two-factor authentication.
  - `admin.py` - Admin login, lockouts, rate limits.
- Supabase service client (`app/services/supabase_service.py`) with:
  - Safe CRUD operations.
  - Admin login & lockout logic.
  - Profile fetching and analytics helpers.
- `constants.py` - For rate limits, lockout thresholds, and roles.

### Fixed
- Module import issues (`ModuleNotFoundError`) with relative imports in `auth` routes.
- Redis connection retry logic.
- Socket.IO integration to work with Redis message queue.
- Logging setup for both development (DEBUG) and production.

### Changed
- Project structure refactored to **modular & maintainable layout**.
- `.env` loading in `main.py` before app creation.
- Debug flag is configurable via `.env`.
- Blueprint registration centralized in `create_app()`.

### Notes
- Redis must be running locally or via configured `REDIS_URL`.
- Supabase service role key required in `.env`.
- Eventlet server used for Socket.IO.
