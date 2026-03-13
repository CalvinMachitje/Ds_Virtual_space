# Auth-Service Microservice Architecture

## Overview
The `auth-service` is a **Flask-based microservice** responsible for:
- Authentication (username/password, OAuth, 2FA)
- Admin user management
- JWT token handling and revocation
- Integration with Supabase for user data and analytics
- Live logging via Redis pub/sub and Socket.IO

---

## Project Structure

auth-service/
├─ app/
│ ├─ init.py # Flask app factory and global configuration
│ ├─ extensions.py # Centralized extensions initialization
│ ├─ routes/
│ │ └─ auth/
│ │ ├─ routes.py # Core authentication endpoints
│ │ ├─ oauth.py # OAuth endpoints
│ │ ├─ twofa.py # Two-factor authentication routes
│ │ └─ admin.py # Admin login and lockout
│ ├─ services/
│ │ └─ supabase_service.py # Supabase client, CRUD helpers, analytics
│ └─ constants.py # Rate limits, lockout thresholds, roles
├─ main.py # Entry point to start the Flask server
├─ .venv/ # Python virtual environment
└─ .env # Environment variables


---

## Core Components

### Flask App Factory (`create_app`)
- Configures JWT, Redis, CORS, Supabase.
- Registers all auth blueprints.
- Sets up global error handler.
- Health check endpoint `/api/health`.
- Handles preflight OPTIONS requests for CORS.

### Redis
- Connection with retry and logging.
- Blocklist for JWT token revocation.
- Socket.IO pub/sub integration for live logs.

### JWT
- Tokens stored in headers.
- Expiration configuration via `.env`.
- Blocklist verification using Redis.

### SupabaseService
- Singleton client.
- CRUD helpers (`get_all`, `get_by_id`, `insert`, `update`, `delete`).
- Admin login with lockout and 2FA.
- Analytics helpers for dashboard/reporting.

### Blueprints
- Modular route separation:
  - `routes` → Standard authentication endpoints
  - `oauth` → OAuth handling
  - `twofa` → Two-factor authentication
  - `admin` → Admin login, lockouts, rate limits

### Security & Performance
- Flask-Talisman for HTTPS and security headers.
- Flask-Caching for Redis-based caching.
- Flask-Limiter for IP-based rate limiting.
- Flask-Compress for response compression.

---

## Dependencies
- Flask
- Flask-JWT-Extended
- Flask-CORS
- Flask-Limiter
- Flask-SocketIO (eventlet)
- Flask-Mail
- Flask-Cache
- Flask-Talisman
- Flask-Migrate
- redis-py
- python-dotenv
- supabase-py

---

## Notes
- Redis must be running and accessible at `REDIS_URL`.
- Supabase service role key required in `.env`.
- All imports use relative paths within `app/` for modularity.
- Logging is centralized via `extensions.py`.

---
