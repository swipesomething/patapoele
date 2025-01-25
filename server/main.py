import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.websockets import WebSocketDisconnect
import pigpio
from datetime import datetime
import socket
from typing import Set, Optional
from fastapi.middleware.cors import CORSMiddleware
import json
from dataclasses import dataclass
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def load_config():
    return {
        "pigpio": {
            "hostname": os.getenv("PIGPIO_HOSTNAME", "localhost"),
            "port": int(os.getenv("PIGPIO_PORT", "8888"))
        },
        "server": {
            "host": os.getenv("SERVER_HOST", "0.0.0.0"),
            "port": int(os.getenv("SERVER_PORT", "8000"))
        },
        "max6675": {
            "spi_channel": int(os.getenv("MAX6675_SPI_CHANNEL", "0")),
            "spi_baud": int(os.getenv("MAX6675_SPI_BAUD", "1000000")),
            "spi_flags": int(os.getenv("MAX6675_SPI_FLAGS", "0"))
        },
        "websocket": {
            "timeout": int(os.getenv("WS_TIMEOUT", "60"))
        },
        "cors": {
            "origins": os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
        },
        "sampling": {
            "rate": float(os.getenv("SAMPLING_RATE", "0.25"))
        }
    }

config = load_config()

def resolve_hostname(hostname):
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    task = asyncio.create_task(temperature_monitor())
    yield
    # Shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

# Update CORS middleware to use configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=config['cors']['origins'],
    allow_methods=["GET", "OPTIONS"],  # Restrict to only needed methods
    allow_headers=["*"],
    max_age=3600,  # Cache preflight requests for 1 hour
)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.connection_count = 0

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"Client {getattr(websocket, 'client_id', 'unknown')} disconnected. "
                  f"Active connections: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        disconnected = set()
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                print(f"Error broadcasting to client: {e}")
                disconnected.add(connection)
        
        # Remove disconnected clients after iteration
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()

class MAX6675Reader:
    def __init__(self, pi):
        self.pi = pi
        self.handle = self.pi.spi_open(
            config['max6675']['spi_channel'],
            config['max6675']['spi_baud'],
            config['max6675']['spi_flags']
        )

    def read_temp(self):
        # Read 16 bits from MAX6675
        count, data = self.pi.spi_xfer(self.handle, [0x00, 0x00])
        if count == 2:
            word = (data[0] << 8) | data[1]
            # Check if bits 15, 2, and 1 are zero (proper reading)
            if (word & 0x8006) == 0:
                temp_c = (word >> 3) / 4.0
                temp_f = (temp_c * 1.8) + 32
                return temp_c, temp_f
        return None, None

    def close(self):
        self.pi.spi_close(self.handle)

@dataclass
class TemperatureReading:
    timestamp: str
    temperature_c: float
    temperature_f: float

current_temperature: Optional[TemperatureReading] = None

async def temperature_monitor():
    global current_temperature
    try:
        pi = pigpio.pi(
            config['pigpio']['hostname'],
            config['pigpio']['port']
        )
        if not pi.connected:
            raise ConnectionError("Failed to connect to local pigpio daemon")
        
        reader = MAX6675Reader(pi)
        
        while True:
            temp_c, temp_f = reader.read_temp()
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
            if temp_c is not None:
                # Store the current reading
                current_temperature = TemperatureReading(
                    timestamp=timestamp,
                    temperature_c=round(temp_c, 2),
                    temperature_f=round(temp_f, 2)
                )
                # Create message for WebSocket clients
                message = json.dumps(vars(current_temperature))
                await manager.broadcast(message)
            await asyncio.sleep(config['sampling']['rate'])  # Use configured sampling rate

    except Exception as e:
        print(f"Error in temperature monitor: {e}")
    finally:
        if 'reader' in locals():
            reader.close()
        if 'pi' in locals() and pi.connected:
            pi.stop()

@app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    try:
        await manager.connect(websocket)
        
        while True:
            try:
                # Set timeout for receiving messages
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=config['websocket']['timeout']
                )
                
                # Validate message format
                try:
                    data = json.loads(message)
                    if not isinstance(data, dict):
                        continue
                    
                    if data.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                except json.JSONDecodeError:
                    continue
                    
            except asyncio.TimeoutError:
                await websocket.close(code=1000)
                break
            except WebSocketDisconnect:
                break
            except Exception as e:
                break
    finally:
        manager.disconnect(websocket)

@app.get("/temperature")
async def get_temperature():
    if current_temperature is None:
        raise HTTPException(status_code=503, detail="Temperature reading not available")
    return vars(current_temperature)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=config['server']['host'],
        port=config['server']['port']
    )
