# services/auth_service/app/utils/webhook_utils.py
import hmac
import hashlib
from flask import request, abort
import os

def verify_webhook_signature():
    webhook_secret = os.getenv("WEBHOOK_SECRET")
    if not webhook_secret:
        abort(500)

    signature = request.headers.get("X-Signature")
    if not signature:
        abort(400)

    expected = hmac.new(
        webhook_secret.encode(),
        request.data,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        abort(403)