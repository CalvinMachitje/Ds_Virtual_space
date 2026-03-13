from flask import Flask, request
import requests

app = Flask(__name__)

SERVICES = {
    "auth": "http://auth-service:5001",
    "users": "http://user-service:5002",
    "admin": "http://admin-service:5003",
    "support": "http://support-service:5004",
}

@app.route("/<service>/<path:path>", methods=["GET","POST","PUT","DELETE"])
def gateway(service, path):

    if service not in SERVICES:
        return {"error": "Service not found"}, 404

    url = f"{SERVICES[service]}/{path}"

    response = requests.request(
        method=request.method,
        url=url,
        headers=request.headers,
        json=request.get_json(silent=True)
    )

    return response.content, response.status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)