# services/auth_service/app/utils/supabase_retry.py
import time
from functools import wraps
import logging

logger = logging.getLogger(__name__)

def retry_supabase(max_retries=3, backoff=2):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "disconnected" in str(e).lower() or "timeout" in str(e).lower():
                        retries += 1
                        wait = backoff ** retries
                        logger.warning(f"Supabase retry {retries}/{max_retries} after {wait}s: {str(e)}")
                        time.sleep(wait)
                    else:
                        raise
            raise Exception(f"Supabase failed after {max_retries} retries")
        return wrapper
    return decorator