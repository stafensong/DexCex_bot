import asyncio
import json
import redis.asyncio as redis

# --- Configuration ---
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
REDIS_CHANNEL = "bot_dashboard_channel"

# --- Redis Connection (Async Lazy Initialization) ---
_redis_client = None
_redis_lock = None  # Initialize lock as None

async def get_redis_client():
    """
    Initializes and returns the async Redis client, creating it if it doesn't exist.
    Uses a lazily-initialized lock to prevent multiple concurrent initializations.
    """
    global _redis_client
    global _redis_lock

    # Initialize the lock inside the async function where a loop is guaranteed to be running
    if _redis_lock is None:
        _redis_lock = asyncio.Lock()

    if _redis_client is None:
        async with _redis_lock:
            # Check again inside the lock in case it was created while waiting.
            if _redis_client is None:
                try:
                    print("[Publisher] Attempting to connect to Redis (async)...")
                    client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
                    await client.ping()
                    print("[Publisher] Successfully connected to Redis (async).")
                    _redis_client = client
                except redis.exceptions.ConnectionError as e:
                    print(f"[Publisher] FATAL: Could not connect to Redis. Dashboard will not receive updates. Error: {e}")
                    return None
    return _redis_client

async def publish_message(source: str, event: str, data):
    """
    Publishes a message to the Redis channel for the dashboard asynchronously.
    """
    client = await get_redis_client()
    if not client:
        return

    try:
        message = {
            "source": source,
            "event": event,
            "data": data
        }
        await client.publish(REDIS_CHANNEL, json.dumps(message, default=str))
    except Exception as e:
        print(f"[Publisher] Error publishing message to Redis: {e}")
