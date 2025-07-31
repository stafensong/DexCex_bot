import asyncio
import json
import redis.asyncio as redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

# --- Configuration ---
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
REDIS_CHANNEL = "bot_dashboard_channel"

# --- FastAPI Application ---
app = FastAPI()

class ConnectionManager:
    """Manages active WebSocket connections."""
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"New client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        print(f"Client disconnected. Total clients: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            await connection.send_text(message)

manager = ConnectionManager()

async def redis_listener(pubsub):
    """Listens for messages from Redis and broadcasts them."""
    while True:
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message:
                print(f"Received from Redis: {message['data'].decode()}")
                await manager.broadcast(message['data'].decode())
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Error in Redis listener: {e}")
            await asyncio.sleep(1)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """The WebSocket endpoint that clients connect to."""
    await manager.connect(websocket)
    try:
        while True:
            # Keep the connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


@app.on_event("startup")
async def startup_event():
    """On server startup, connect to Redis and start the listener task."""
    print("Server starting up...")
    try:
        redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, auto_close_connection_pool=False)
        pubsub = redis_client.pubsub()
        await pubsub.subscribe(REDIS_CHANNEL)
        print(f"Subscribed to Redis channel: {REDIS_CHANNEL}")
        # Start the listener as a background task
        asyncio.create_task(redis_listener(pubsub))
    except Exception as e:
        print(f"FATAL: Could not connect to Redis on startup. {e}")
        # In a real app, you might want to exit or handle this more gracefully
        
@app.get("/", response_class=HTMLResponse)
async def get():
    # Simple health check page
    return """
    <!DOCTYPE html>
    <html>
        <head>
            <title>Bot Dashboard Backend</title>
        </head>
        <body>
            <h1>Dashboard Backend is Running</h1>
            <p>Connect your WebSocket client to <strong>/ws</strong>.</p>
        </body>
    </html>
    """

if __name__ == "__main__":
    print("Starting Uvicorn server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
