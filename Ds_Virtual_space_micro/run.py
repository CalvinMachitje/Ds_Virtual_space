# This is the entry point for the Auth Service microservice. It sets up a simple Flask application with two endpoints: /health for health checks and /test for testing the service. The service runs on port 5001.
# root directory: Ds_Virtual_space_micro\run.py
from flask import Flask
print("🚀 D's Virtual Space Auth Service Starting...")

app = Flask(__name__)

@app.route('test')
def health():
    return {'status': 'healthy', 'service': 'auth-service', 'version': '1.0.0-micro'}

@app.route('/test')
def test():
    return {'message': '🎉 MICROSERVICE #1 LIVE ON PORT 5001!'}

print("🌐 Auth Service LIVE on http://localhost:5001")
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
