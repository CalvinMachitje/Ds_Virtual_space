# server/app.py

import eventlet

# Apply eventlet monkey patch **FIRST** — before ANY other imports
eventlet.monkey_patch(all=True, thread=True, os=True, socket=True, select=True, time=True)

# Now safe to import everything
from flask import Flask, request
from app import create_app
from app.extensions import socketio

# Create the actual application instance
app = create_app()

if __name__ == "__main__":
    # For development only — use Gunicorn + eventlet in production
    print("Starting Flask-SocketIO development server on http://0.0.0.0:5000")
    
    # Use socketio.run() to properly handle WebSocket + eventlet
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=True,
        use_reloader=True,
        allow_unsafe_werkzeug=True,  # Suppress Werkzeug deprecation noise in dev
        log_output=True
    )

@app.route("/debug-cors", methods=["OPTIONS", "GET"])
def debug_cors():
    if request.method == "OPTIONS":
        resp = app.make_response(('', 204))
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp
    return {"message": "GET works", "origin": request.headers.get("Origin")}