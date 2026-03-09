# services/auth_service/app/__init__.py
import os
import sys
import pathlib
from flask import Flask

from dotenv import load_dotenv

# 🔧 FIX #1: Add CURRENT DIRECTORY to Python path (you're running from app/)
sys.path.insert(0, str(pathlib.Path(__file__).parent))

load_dotenv()

def create_app():
    app = Flask(__name__)
    
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-secret"),
        JWT_SECRET_KEY=os.getenv("JWT_SECRET_KEY", "dev-jwt"),
        JWT_ACCESS_TOKEN_EXPIRES=45*60,
        REDIS_URL=os.getenv("REDIS_URL", "redis://localhost:6379"),
        FRONTEND_ORIGINS=["http://localhost:5173", "http://localhost:3000"],
    )
    
    # 🔧 FIX #2: Import AFTER path is fixed
    from extensions import init_extensions
    init_extensions(app)
    
    # 🔧 FIX #3: Import routes AFTER path is fixed
    from routes.auth import bp as auth_bp
    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    
    @app.route("/health")
    def health():
        return {
            "status": "healthy",
            "service": "auth-service", 
            "version": "1.0.0-micro",
            "endpoints": ["/api/auth/*"]
        }
    
    return app

if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5001, debug=True)
