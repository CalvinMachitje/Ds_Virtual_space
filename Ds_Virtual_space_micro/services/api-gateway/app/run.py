# services\api-gateway\app\run.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
from dotenv import load_dotenv

print("🚀 D's Virtual Space API GATEWAY Starting...")

load_dotenv()
app = Flask(__name__)
CORS(app, origins=["http://localhost:3000", "http://localhost:5173"])

SERVICES = {
    'auth': 'http://localhost:5001',
    'gigs': 'http://localhost:5002',
    'buyer': 'http://localhost:5003',
    'admin': 'http://localhost:5004'
}

@app.route('/health')
def health():
    return {
        'status': 'healthy',
        'gateway': 'v1.0.0',
        'services': list(SERVICES.keys())
    }

@app.route('/test')
def test():
    return {'message': '🎉 API GATEWAY LIVE ON PORT 5000!'}

@app.route('/api/<service>/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def proxy_to_service(service, path):
    if service not in SERVICES:
        return jsonify({'error': f'Service "{service}" not found', 'available': list(SERVICES.keys())}), 404
    
    target_url = f"{SERVICES[service]}/api/{service}/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != 'host'}
    
    try:
        # FIXED: requests.request() not request.request()
        response = requests.request(  # ← CORRECT: 'requests' library
            method=request.method,
            url=target_url,
            headers=headers,
            json=request.get_json(),
            params=request.args,
            timeout=30
        )
        return response.content, response.status_code, dict(response.headers)
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Microservice {service} down: {str(e)}'}), 503

print("🌐 API Gateway LIVE on http://localhost:5000")
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
