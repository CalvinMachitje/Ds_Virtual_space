# test_redis.py
import redis
import os

redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")

print(f"Trying to connect to: {redis_url}")

try:
    r = redis.from_url(redis_url, decode_responses=True)
    pong = r.ping()
    print("SUCCESS: Redis ping returned", pong)
    print("Redis info:", r.info("server"))
except Exception as e:
    print("FAILED to connect:", str(e))