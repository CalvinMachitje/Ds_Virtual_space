# Save as test_httpx.py and run: python test_httpx.py
import httpx

print("httpx version:", httpx.__version__)

client = httpx.Client(http2=True)
resp = client.get("https://httpbin.org/anything", timeout=10)
print("Protocol used:", resp.http_version)  # Should print HTTP/2 if successful