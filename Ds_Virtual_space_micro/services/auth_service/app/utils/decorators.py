# services/auth_service/app/utils/decorators.py
from functools import wraps
from flask import jsonify
from flask_jwt_extended import get_jwt_identity
from supabase_service import supabase

def admin_required(f):
    """
    Decorator: Ensures the current user is an admin.
    Checks the 'admins' table (not profiles.role).
    Returns 403 if not found in admins table or token invalid.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        identity = get_jwt_identity()
        if not identity:
            return jsonify({"error": "Authentication required"}), 401

        try:
            admin_record = supabase.table("admins")\
                .select("admin_level")\
                .eq("id", identity)\
                .maybe_single().execute()

            if not admin_record.data:
                return jsonify({"error": "Admin access required"}), 403

            return f(*args, **kwargs)

        except Exception as e:
            print(f"Admin check failed: {str(e)}")  # log in dev
            return jsonify({"error": "Failed to verify permissions"}), 500

    return decorated_function