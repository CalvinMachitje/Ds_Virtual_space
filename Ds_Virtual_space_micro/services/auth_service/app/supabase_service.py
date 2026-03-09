# services/auth_service/app/supabase_service.py
"""
Central Supabase client for backend (uses service_role key → full access).
"""
import os
import logging
from typing import Any, Dict, List, Optional, Callable
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

class SupabaseService:
    def __init__(self):
        # ✅ CORRECT Backend env vars (NO VITE_ prefix)
        url = os.getenv("VITE_SUPABASE_URL")
        key = os.getenv("VITE_SUPABASE_SERVICE_ROLE_KEY")

        if not url:
            raise ValueError("VITE_SUPABASE_URL is missing from .env")
        if not key:
            raise ValueError("VITE_SUPABASE_SERVICE_ROLE_KEY is missing from .env")

        self.client: Client = create_client(url, key)
        self.auth = self.client.auth
        self.table = self.client.table
        self.storage = self.client.storage
        
        logger.info("✅ Supabase client initialized (service_role)")

    # Simplified CRUD (Redis disabled for now)
    def get_by_id(self, table: str, id: str, select: str = "*") -> Optional[Dict]:
        try:
            result = self.client.table(table).select(select).eq("id", id).execute()
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"get_by_id failed on {table}/{id}: {e}")
            return None

    def admin_login(self, email: str, password: str) -> Dict[str, Any]:
        """Simplified admin login (no Redis/2FA for now)"""
        try:
            auth_res = self.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
            
            if auth_res.get("user") and auth_res.get("session"):
                return {"success": True, "user": auth_res["user"], "session": auth_res["session"]}
            else:
                return {"success": False, "error": auth_res.get("error", "Login failed")}
                
        except Exception as e:
            logger.error(f"Admin login failed: {e}")
            return {"success": False, "error": "Authentication server error"}

# Singleton instance
supabase = SupabaseService()
