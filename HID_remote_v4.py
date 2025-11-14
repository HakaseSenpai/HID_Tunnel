#!/usr/bin/env python3
"""
MQTT HID Forwarder v4.0 - Phase 1: Critical Fixes + WebSocket Support

Key improvements in v4:
- Fixed MQTT reconnection with exponential backoff
- Thread-safe shared state with locks
- Connection state feedback (enum + logging)
- Protocol negotiation for keyboard state
- Transport abstraction layer
- WebSocket transport support
- Basic discovery/locked mode

Based on HID_remote.py v3.7 with critical reliability improvements.
"""
from __future__ import annotations
import argparse
import json
import queue
import signal
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

import paho.mqtt.client as mqtt


# ═══════════════════════════════════════════════════════════════════════════
# CONNECTION STATE & TYPES
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionState(Enum):
    """Connection state for clear user feedback."""
    NO_TRANSPORTS = "no_transports"      # No transports configured
    DISCOVERING = "discovering"          # Looking for device
    ACTIVE = "active"                    # Connected and sending HID
    DEGRADED = "degraded"                # Connected but unstable
    LOCKED = "locked"                    # Locked to a specific transport


@dataclass
class TransportStatus:
    """Status for a single transport endpoint."""
    last_seen: float = 0.0
    device_online: bool = False
    last_connect_attempt: float = 0.0
    connect_failures: int = 0
    avg_latency_ms: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# TRANSPORT ABSTRACTION
# ═══════════════════════════════════════════════════════════════════════════

class HIDTransport(ABC):
    """Abstract base class for HID transport methods."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.on_status_callback = None

    @abstractmethod
    def connect(self) -> bool:
        """Attempt to connect. Returns True on success."""
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        """Check if currently connected."""
        pass

    @abstractmethod
    def disconnect(self):
        """Gracefully disconnect."""
        pass

    @abstractmethod
    def send_mouse(self, command: dict):
        """Send mouse command."""
        pass

    @abstractmethod
    def send_key(self, command: dict):
        """Send keyboard command."""
        pass

    @abstractmethod
    def send_ping(self, metadata: Optional[dict] = None):
        """Send ping with optional metadata."""
        pass

    @abstractmethod
    def get_transport_name(self) -> str:
        """Get human-readable transport name."""
        pass


# ═══════════════════════════════════════════════════════════════════════════
# MQTT TRANSPORT
# ═══════════════════════════════════════════════════════════════════════════

class MQTTTransport(HIDTransport):
    """MQTT transport with multi-broker support and reconnection."""

    def __init__(self, device_id: str, brokers: List[Tuple[str, int]]):
        super().__init__(device_id)
        self.brokers = brokers
        self.clients: Dict[str, mqtt.Client] = {}
        self.broker_statuses: Dict[str, TransportStatus] = {}
        self.active_broker: Optional[str] = None
        self.lock = threading.Lock()

        # Topics
        self.mouse_topic = f"hid/{device_id}/mouse"
        self.key_topic = f"hid/{device_id}/key"
        self.status_topic = f"hid/{device_id}/status"
        self.ping_topic = f"hid/{device_id}/ping"

        # Reconnection state
        self.reconnect_delays: Dict[str, float] = {}  # broker_key -> delay
        self.max_reconnect_delay = 60.0  # Max 60 seconds between reconnects

        self._setup_clients()

    def _setup_clients(self):
        """Setup MQTT clients for all brokers."""
        for broker_host, broker_port in self.brokers:
            broker_key = f"{broker_host}:{broker_port}"
            self.broker_statuses[broker_key] = TransportStatus()
            self.reconnect_delays[broker_key] = 1.0  # Start with 1 second

            try:
                client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"{self.device_id}_host_{broker_key}"
                )
                client.user_data_set(broker_key)
                client.on_connect = self._on_connect
                client.on_disconnect = self._on_disconnect
                client.on_message = self._on_message

                self.clients[broker_key] = client
                print(f"[MQTT] Configured client for {broker_key}")
            except Exception as e:
                print(f"[MQTT] Failed to create client for {broker_key}: {e}")

    def connect(self) -> bool:
        """Connect to all configured brokers."""
        success = False
        for broker_key, client in self.clients.items():
            if self._connect_broker(broker_key, client):
                success = True
        return success

    def _connect_broker(self, broker_key: str, client: mqtt.Client) -> bool:
        """Connect to a specific broker with backoff tracking."""
        host, port = broker_key.split(':')
        port = int(port)

        try:
            print(f"[MQTT] Connecting to {broker_key}...")
            client.connect(host, port, 60)
            client.loop_start()
            return True
        except Exception as e:
            print(f"[MQTT] Connection to {broker_key} failed: {e}")
            with self.lock:
                self.broker_statuses[broker_key].connect_failures += 1
                self.broker_statuses[broker_key].last_connect_attempt = time.time()
            return False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Called when broker connection established."""
        broker_key = userdata
        print(f"[MQTT] ✓ Connected to {broker_key} (rc={rc})")

        with self.lock:
            # Reset reconnect delay on successful connect
            self.reconnect_delays[broker_key] = 1.0
            self.broker_statuses[broker_key].connect_failures = 0

        # Subscribe to status and ping topics
        client.subscribe(self.status_topic, qos=1)
        client.subscribe(self.ping_topic, qos=1)

        # Send discovery ping
        self.send_ping(metadata={"broker": broker_key})

    def _on_disconnect(self, client, userdata, rc, properties=None):
        """Called when disconnected from broker."""
        broker_key = userdata
        print(f"[MQTT] ✗ Disconnected from {broker_key} (rc={rc})")

        with self.lock:
            # If this was the active broker, clear it
            if self.active_broker == broker_key:
                print(f"[MQTT] Lost active broker {broker_key}")
                self.active_broker = None

        # Schedule reconnection with exponential backoff
        self._schedule_reconnect(broker_key, client)

    def _schedule_reconnect(self, broker_key: str, client: mqtt.Client):
        """Schedule reconnection attempt with exponential backoff."""
        def reconnect_worker():
            delay = self.reconnect_delays.get(broker_key, 1.0)
            print(f"[MQTT] Will retry {broker_key} in {delay:.1f}s...")
            time.sleep(delay)

            if not client.is_connected():
                print(f"[MQTT] Attempting reconnect to {broker_key}...")
                try:
                    client.reconnect()
                    # On success, loop_start is already running
                except Exception as e:
                    print(f"[MQTT] Reconnect to {broker_key} failed: {e}")
                    # Exponential backoff
                    new_delay = min(delay * 2, self.max_reconnect_delay)
                    self.reconnect_delays[broker_key] = new_delay
                    # Try again
                    self._schedule_reconnect(broker_key, client)

        # Start reconnect thread
        threading.Thread(target=reconnect_worker, daemon=True).start()

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages."""
        broker_key = userdata
        try:
            payload = json.loads(msg.payload.decode())

            if msg.topic == self.status_topic:
                self._handle_status(broker_key, payload)
        except Exception as e:
            print(f"[MQTT] Error processing message from {broker_key}: {e}")

    def _handle_status(self, broker_key: str, payload: dict):
        """Handle status message from device."""
        if payload.get("status") in ["online", "alive"]:
            with self.lock:
                self.broker_statuses[broker_key].last_seen = time.time()
                self.broker_statuses[broker_key].device_online = True

                # If no active broker, select this one
                if self.active_broker is None:
                    self.active_broker = broker_key
                    print(f"[MQTT] ✓✓ Device discovered on {broker_key} - now active")

                    # Notify callback
                    if self.on_status_callback:
                        self.on_status_callback(payload)

    def is_connected(self) -> bool:
        """Check if any broker is connected and has active device."""
        with self.lock:
            return self.active_broker is not None

    def disconnect(self):
        """Disconnect all MQTT clients."""
        for broker_key, client in self.clients.items():
            try:
                client.loop_stop()
                client.disconnect()
                print(f"[MQTT] Disconnected from {broker_key}")
            except:
                pass

    def send_mouse(self, command: dict):
        """Send mouse command via active broker."""
        with self.lock:
            if self.active_broker and self.active_broker in self.clients:
                client = self.clients[self.active_broker]
                client.publish(self.mouse_topic, json.dumps(command), qos=0)

    def send_key(self, command: dict):
        """Send keyboard command via active broker."""
        with self.lock:
            if self.active_broker and self.active_broker in self.clients:
                client = self.clients[self.active_broker]
                client.publish(self.key_topic, json.dumps(command), qos=1)

    def send_ping(self, metadata: Optional[dict] = None):
        """Send ping to all connected brokers."""
        ping_msg = {
            "from": "host",
            "device_id": self.device_id,
            "timestamp": time.time()
        }
        if metadata:
            ping_msg.update(metadata)

        ping_json = json.dumps(ping_msg)
        for broker_key, client in self.clients.items():
            try:
                if client.is_connected():
                    client.publish(self.ping_topic, ping_json, qos=1)
            except:
                pass

    def get_transport_name(self) -> str:
        with self.lock:
            if self.active_broker:
                return f"mqtt://{self.active_broker}"
            return "mqtt://(discovering)"

    def check_health(self):
        """Check health of active broker and trigger rediscovery if needed."""
        with self.lock:
            if self.active_broker:
                status = self.broker_statuses.get(self.active_broker)
                if status and time.time() - status.last_seen > 10.0:
                    print(f"[MQTT] Active broker {self.active_broker} timed out")
                    self.active_broker = None


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET TRANSPORT
# ═══════════════════════════════════════════════════════════════════════════

class WebSocketTransport(HIDTransport):
    """WebSocket transport - host acts as server, ESP32 connects as client."""

    def __init__(self, device_id: str, host: str = "0.0.0.0", port: int = 8765):
        super().__init__(device_id)
        self.host = host
        self.port = port
        self.server = None
        self.client_ws = None
        self.connected = False
        self.lock = threading.Lock()
        self.last_seen = 0.0

        # Try to import websockets
        try:
            import websockets
            self.websockets = websockets
        except ImportError:
            print("[WS] websockets library not available. Install: pip install websockets")
            self.websockets = None

    def connect(self) -> bool:
        """Start WebSocket server."""
        if self.websockets is None:
            return False

        try:
            import asyncio

            async def handler(websocket, path):
                """Handle incoming WebSocket connection."""
                print(f"[WS] Client connected from {websocket.remote_address}")
                with self.lock:
                    self.client_ws = websocket
                    self.connected = True
                    self.last_seen = time.time()

                try:
                    async for message in websocket:
                        await self._handle_message(message)
                except Exception as e:
                    print(f"[WS] Connection error: {e}")
                finally:
                    with self.lock:
                        self.client_ws = None
                        self.connected = False
                    print("[WS] Client disconnected")

            async def start_server():
                async with self.websockets.serve(handler, self.host, self.port):
                    print(f"[WS] Server listening on ws://{self.host}:{self.port}")
                    await asyncio.Future()  # Run forever

            def run_server():
                asyncio.run(start_server())

            # Start server in background thread
            threading.Thread(target=run_server, daemon=True).start()
            time.sleep(0.5)  # Give server time to start
            return True

        except Exception as e:
            print(f"[WS] Failed to start server: {e}")
            return False

    async def _handle_message(self, message):
        """Handle incoming WebSocket message from device."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "status":
                with self.lock:
                    self.last_seen = time.time()
                if self.on_status_callback:
                    self.on_status_callback(data)
            elif msg_type == "pong":
                with self.lock:
                    self.last_seen = time.time()
        except Exception as e:
            print(f"[WS] Error handling message: {e}")

    def is_connected(self) -> bool:
        with self.lock:
            return self.connected and self.client_ws is not None

    def disconnect(self):
        """Close WebSocket server."""
        with self.lock:
            if self.client_ws:
                # Note: Proper async close would be better, but keeping simple
                self.client_ws = None
            self.connected = False

    def _send_ws(self, data: dict):
        """Send data over WebSocket (synchronous wrapper for async send)."""
        with self.lock:
            if self.client_ws and self.connected:
                try:
                    import asyncio
                    # Get or create event loop
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)

                    # Send message
                    asyncio.ensure_future(self.client_ws.send(json.dumps(data)))
                except Exception as e:
                    print(f"[WS] Send error: {e}")

    def send_mouse(self, command: dict):
        command["type"] = "mouse"
        self._send_ws(command)

    def send_key(self, command: dict):
        command["type"] = "key"
        self._send_ws(command)

    def send_ping(self, metadata: Optional[dict] = None):
        ping = {"type": "ping", "device_id": self.device_id, "timestamp": time.time()}
        if metadata:
            ping.update(metadata)
        self._send_ws(ping)

    def get_transport_name(self) -> str:
        return f"ws://{self.host}:{self.port}"


# ═══════════════════════════════════════════════════════════════════════════
# TRANSPORT MANAGER
# ═══════════════════════════════════════════════════════════════════════════

class TransportManager:
    """Manages multiple transports with discovery and locking."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.transports: List[HIDTransport] = []
        self.active_transport: Optional[HIDTransport] = None
        self.connection_state = ConnectionState.NO_TRANSPORTS
        self.lock = threading.Lock()

        # Keyboard state
        self.use_keyboard_state = False
        self.currently_pressed: Set[int] = set()
        self.keyboard_lock = threading.Lock()

        # Mouse state
        self.pending_mouse_dx = 0
        self.pending_mouse_dy = 0
        self.pending_mouse_wheel = 0
        self.mouse_lock = threading.Lock()

        # Timing
        self.last_activity_time = time.time()
        self.last_key_time = time.time()
        self.last_send_time = time.time()
        self.rate_limit_ms = 20
        self.sensitivity = 0.5
        self.alpha = 0.5
        self.smoothed_dx = 0.0
        self.smoothed_dy = 0.0

        # Start background threads
        threading.Thread(target=self._timeout_handler, daemon=True).start()
        threading.Thread(target=self._discovery_handler, daemon=True).start()

    def add_transport(self, transport: HIDTransport):
        """Add a transport to the manager."""
        transport.on_status_callback = self._on_transport_status
        self.transports.append(transport)
        print(f"[TRANSPORT] Added: {transport.get_transport_name()}")

    def connect_all(self):
        """Attempt to connect all transports."""
        if not self.transports:
            self.connection_state = ConnectionState.NO_TRANSPORTS
            print("[TRANSPORT] ⚠ No transports configured!")
            return

        self.connection_state = ConnectionState.DISCOVERING
        print(f"[TRANSPORT] Connecting {len(self.transports)} transport(s)...")

        for transport in self.transports:
            try:
                transport.connect()
            except Exception as e:
                print(f"[TRANSPORT] Failed to connect {transport.get_transport_name()}: {e}")

    def _on_transport_status(self, status: dict):
        """Callback when a transport receives status from device."""
        # Select first transport that reports device online
        with self.lock:
            if self.active_transport is None:
                for transport in self.transports:
                    if transport.is_connected():
                        self.active_transport = transport
                        self.connection_state = ConnectionState.ACTIVE
                        print(f"[TRANSPORT] ✓✓ Active transport: {transport.get_transport_name()}")

                        # Send release_all for clean state
                        self.send_key_command("release_all", 0)
                        break

    def _timeout_handler(self):
        """Background thread: handle inactivity timeouts."""
        inactivity_timeout_s = 2
        while True:
            now = time.time()
            if now - self.last_key_time > inactivity_timeout_s:
                self.send_key_command("release_all", 0)
                self.last_key_time = now
            time.sleep(0.5)

    def _discovery_handler(self):
        """Background thread: periodic pings and health checks."""
        while True:
            time.sleep(3)

            # Send periodic pings
            for transport in self.transports:
                try:
                    if transport.is_connected():
                        transport.send_ping()
                except Exception as e:
                    print(f"[TRANSPORT] Ping error on {transport.get_transport_name()}: {e}")

            # Check health
            with self.lock:
                if self.active_transport:
                    if not self.active_transport.is_connected():
                        print(f"[TRANSPORT] Active transport {self.active_transport.get_transport_name()} lost")
                        self.active_transport = None
                        self.connection_state = ConnectionState.DISCOVERING

    def _should_send(self) -> bool:
        """Rate limiting check."""
        now = time.time()
        if now - self.last_send_time >= self.rate_limit_ms / 1000.0:
            self.last_send_time = now
            return True
        return False

    def _smooth_and_scale(self, dx, dy):
        """Apply EMA smoothing and sensitivity scaling."""
        self.smoothed_dx = self.alpha * dx + (1 - self.alpha) * self.smoothed_dx
        self.smoothed_dy = self.alpha * dy + (1 - self.alpha) * self.smoothed_dy
        return int(self.smoothed_dx * self.sensitivity), int(self.smoothed_dy * self.sensitivity)

    def send_mouse_command(self, dx=0, dy=0, wheel=0, button=None, button_action=None):
        """Send mouse command with accumulation and rate limiting."""
        with self.lock:
            if not self.active_transport:
                return  # Silently drop if no active transport

        # Force send for button actions
        force = bool(button and button_action)

        if not force and not self._should_send():
            # Accumulate movement
            if dx or dy or wheel:
                with self.mouse_lock:
                    self.pending_mouse_dx += dx
                    self.pending_mouse_dy += dy
                    self.pending_mouse_wheel += wheel
            return

        # Get accumulated movement
        with self.mouse_lock:
            if dx or dy or wheel:
                self.pending_mouse_dx += dx
                self.pending_mouse_dy += dy
                self.pending_mouse_wheel += wheel
            final_dx = self.pending_mouse_dx
            final_dy = self.pending_mouse_dy
            final_wheel = self.pending_mouse_wheel
            self.pending_mouse_dx = 0
            self.pending_mouse_dy = 0
            self.pending_mouse_wheel = 0

        scaled_dx, scaled_dy = self._smooth_and_scale(final_dx, final_dy)
        command = {
            "dx": scaled_dx,
            "dy": scaled_dy,
            "wheel": final_wheel,
            "timestamp": time.time()
        }

        if button and button_action:
            command["button"] = button
            command["button_action"] = button_action

        with self.lock:
            if self.active_transport:
                self.active_transport.send_mouse(command)
                self.last_activity_time = time.time()

    def send_key_command(self, action: str, key_code: int):
        """Send keyboard command with state tracking."""
        with self.lock:
            if not self.active_transport:
                return

        if self.use_keyboard_state:
            # State-based protocol
            with self.keyboard_lock:
                if action == "press":
                    self.currently_pressed.add(key_code)
                elif action == "release":
                    self.currently_pressed.discard(key_code)
                elif action == "release_all":
                    self.currently_pressed.clear()

                command = {
                    "action": "state",
                    "pressed": list(self.currently_pressed),
                    "timestamp": time.time()
                }
        else:
            # Legacy event-based protocol
            command = {
                "action": action,
                "key": key_code,
                "timestamp": time.time()
            }

        with self.lock:
            if self.active_transport:
                self.active_transport.send_key(command)
                self.last_activity_time = time.time()
                self.last_key_time = time.time()

    def get_connection_state(self) -> ConnectionState:
        """Get current connection state."""
        with self.lock:
            return self.connection_state

    def get_active_transport_name(self) -> str:
        """Get name of active transport."""
        with self.lock:
            if self.active_transport:
                return self.active_transport.get_transport_name()
            return f"[{self.connection_state.value}]"

    def shutdown(self):
        """Shutdown all transports."""
        print("[TRANSPORT] Shutting down...")
        for transport in self.transports:
            try:
                transport.disconnect()
            except Exception as e:
                print(f"[TRANSPORT] Error disconnecting {transport.get_transport_name()}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCE (for backends)
# ═══════════════════════════════════════════════════════════════════════════

transport_manager: Optional[TransportManager] = None


def api_get(base: str, path: str, dbg: bool, timeout: float = 1.5) -> bytes:
    """Legacy API compatibility layer for input backends."""
    global transport_manager

    if not transport_manager:
        return b""

    try:
        if "/mouse?" in path:
            params = {}
            if "dx=" in path: params["dx"] = int(path.split("dx=")[1].split("&")[0])
            if "dy=" in path: params["dy"] = int(path.split("dy=")[1].split("&")[0])
            if "wheel=" in path: params["wheel"] = int(path.split("wheel=")[1].split("&")[0])
            button = None
            button_action = None
            if "button=" in path: button = path.split("button=")[1].split("&")[0]
            if "button_action=" in path: button_action = path.split("button_action=")[1].split("&")[0] or path.split("button_action=")[1]

            transport_manager.send_mouse_command(
                params.get("dx", 0),
                params.get("dy", 0),
                params.get("wheel", 0),
                button=button,
                button_action=button_action
            )

        elif "/key?" in path:
            if "press=" in path:
                key_code = int(path.split("press=")[1].split("&")[0])
                transport_manager.send_key_command("press", key_code)
            elif "release=" in path:
                key_code = int(path.split("release=")[1].split("&")[0])
                transport_manager.send_key_command("release", key_code)
            else:
                transport_manager.send_key_command("release_all", 0)

        if dbg:
            print(f"→ API: {path}")

    except Exception as e:
        if dbg:
            print(f"[API] {e} ← {path}")

    return b"OK"


# ═══════════════════════════════════════════════════════════════════════════
# KEY CODE LOOKUP TABLES (unchanged from v3.7)
# ═══════════════════════════════════════════════════════════════════════════

EV2HID: dict[int, int] = {
    1: 0xB1,  # Esc
    2: ord('1'),  3: ord('2'),  4: ord('3'),  5: ord('4'),  6: ord('5'),
    7: ord('6'),  8: ord('7'),  9: ord('8'), 10: ord('9'), 11: ord('0'),
    12: ord('-'), 13: ord('='),
    14: 0xB2,  15: 0xB3, 28: 0xB0,
    29: 0x80,  42: 0x81, 54: 0x85,
    56: 0x82,  57: ord(' '),
    16: ord('q'), 17: ord('w'), 18: ord('e'), 19: ord('r'), 20: ord('t'),
    21: ord('y'), 22: ord('u'), 23: ord('i'), 24: ord('o'), 25: ord('p'),
    26: ord('['), 27: ord(']'),
    30: ord('a'), 31: ord('s'), 32: ord('d'), 33: ord('f'), 34: ord('g'),
    35: ord('h'), 36: ord('j'), 37: ord('k'), 38: ord('l'),
    39: ord(';'), 40: ord("'"), 41: ord('`'), 43: ord('\\'),
    44: ord('z'), 45: ord('x'), 46: ord('c'), 47: ord('v'), 48: ord('b'),
    49: ord('n'), 50: ord('m'),
    51: ord(','), 52: ord('.'), 53: ord('/'),
   105: 0xD8, 106: 0xD7, 103: 0xDA, 108: 0xD9,
   111: 0xD4,
    59: 0xC2, 60: 0xC3, 61: 0xC4, 62: 0xC5, 63: 0xC6,
    64: 0xC7, 65: 0xC8, 66: 0xC9, 67: 0xCA, 68: 0xCB,
    87: 0xCC, 88: 0xCD,
}


def vk2hid(vk: int) -> int | None:
    if 0x30 <= vk <= 0x39:
        return vk
    if 0x41 <= vk <= 0x5A:
        return vk + 32
    if vk in (0x25, 0x27, 0x26, 0x28):
        return {0x25:0xD8, 0x27:0xD7, 0x26:0xDA, 0x28:0xD9}[vk]
    if 0x70 <= vk <= 0x7B:
        return 0xC2 + (vk - 0x70)
    if vk == 0x2E:
        return 0xD4
    return EV2HID.get(vk)


# ═══════════════════════════════════════════════════════════════════════════
# INPUT BACKENDS (unchanged from v3.7, just use global transport_manager)
# ═══════════════════════════════════════════════════════════════════════════

def start_evdev(base: str, dbg: bool) -> bool:
    """Linux evdev backend."""
    try:
        from evdev import InputDevice, categorize, ecodes, list_devices
    except ImportError:
        return False

    devs: list[InputDevice] = []
    for path in list_devices():
        try:
            d = InputDevice(path)
            caps = d.capabilities()
            if ecodes.EV_REL in caps or ecodes.EV_ABS in caps:
                devs.append(d)
        except Exception:
            pass
    if not devs:
        return False
    print(f"✔ evdev backend – {len(devs)} device(s)")

    q: "queue.SimpleQueue" = queue.SimpleQueue()

    def reader(dev: InputDevice):
        for ev in dev.read_loop():
            q.put(ev)
    for d in devs:
        threading.Thread(target=reader, args=(d,), daemon=True).start()

    def mixer():
        dx = dy = wheel = 0
        last_flush = time.time()
        last_abs_x = last_abs_y = None
        while True:
            try:
                ev = q.get(timeout=0.03)
            except queue.Empty:
                pass
            else:
                if ev.type == ecodes.EV_REL:
                    if ev.code == ecodes.REL_X:    dx += ev.value
                    elif ev.code == ecodes.REL_Y:    dy += ev.value
                    elif ev.code == ecodes.REL_WHEEL: wheel += ev.value
                elif ev.type == ecodes.EV_ABS:
                    if ev.code == ecodes.ABS_X:
                        if last_abs_x is not None:
                            dx += ev.value - last_abs_x
                        last_abs_x = ev.value
                    elif ev.code == ecodes.ABS_Y:
                        if last_abs_y is not None:
                            dy += ev.value - last_abs_y
                        last_abs_y = ev.value
                elif ev.type == ecodes.EV_KEY:
                    if ev.code == ecodes.BTN_LEFT:
                        button = "left"
                        action = "press" if ev.value == 1 else "release"
                        api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
                    elif ev.code == ecodes.BTN_RIGHT:
                        button = "right"
                        action = "press" if ev.value == 1 else "release"
                        api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
                    elif ev.code == ecodes.BTN_MIDDLE:
                        button = "middle"
                        action = "press" if ev.value == 1 else "release"
                        api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
                    else:
                        hid = EV2HID.get(ev.code)
                        if hid:
                            api_get(base, f"/key?{'press' if ev.value else 'release'}={hid}", dbg)
            if (dx or dy or wheel) and time.time() - last_flush > 0.04:
                while dx or dy or wheel:
                    step_x = max(-127, min(127, dx))
                    step_y = max(-127, min(127, dy))
                    step_w = max(-127, min(127, wheel))
                    api_get(base, f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}", dbg)
                    dx    -= step_x
                    dy    -= step_y
                    wheel -= step_w
                last_flush = time.time()
    threading.Thread(target=mixer, daemon=True).start()
    return True


def start_pynput(base: str, dbg: bool) -> bool:
    """pynput backend."""
    try:
        from pynput import mouse, keyboard
    except Exception:
        return False

    dx = dy = wheel = 0
    last_flush = time.time()

    def flush():
        nonlocal dx, dy, wheel, last_flush
        while dx or dy or wheel:
            step_x = max(-127, min(127, int(round(dx))))
            step_y = max(-127, min(127, int(round(dy))))
            step_w = max(-127, min(127, int(round(wheel))))
            api_get(base, f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}", dbg)
            dx    -= step_x
            dy    -= step_y
            wheel -= step_w
        last_flush = time.time()

    last_xy = [None, None]

    def on_move(x, y):
        nonlocal dx, dy
        if last_xy[0] is not None:
            dx += (x - last_xy[0]) * 0.1
            dy += (y - last_xy[1]) * 0.1
        last_xy[:] = [x, y]
        if time.time() - last_flush > 0.04:
            flush()

    def on_scroll(_x, _y, _dx, _dy):
        nonlocal wheel
        wheel += _dy
        flush()

    def on_click(x, y, button, pressed):
        button_str = {mouse.Button.left: "left", mouse.Button.right: "right", mouse.Button.middle: "middle"}.get(button)
        if button_str:
            action = "press" if pressed else "release"
            transport_manager.send_mouse_command(dx=0, dy=0, wheel=0, button=button_str, button_action=action)

    mouse.Listener(on_move=on_move, on_scroll=on_scroll, on_click=on_click).start()

    def on_press(k):
        vk = getattr(k, "vk", getattr(k, "value", k).vk)
        hid = vk2hid(vk)
        if hid:
            api_get(base, f"/key?press={hid}", dbg)

    def on_release(k):
        vk = getattr(k, "vk", getattr(k, "value", k).vk)
        hid = vk2hid(vk)
        if hid:
            api_get(base, f"/key?release={hid}", dbg)

    keyboard.Listener(on_press=on_press, on_release=on_release).start()
    print("✔ pynput backend")
    return True


def start_pyautogui(base: str, dbg: bool) -> bool:
    """pyautogui fallback backend."""
    try:
        import pyautogui
    except Exception:
        return False

    dx = dy = wheel = 0
    last_flush = time.time()
    last_pos = pyautogui.position()

    def loop():
        nonlocal dx, dy, wheel, last_flush, last_pos
        last_left_state  = pyautogui.mouseDown(button='left')
        last_right_state = pyautogui.mouseDown(button='right')
        while True:
            pos = pyautogui.position()
            dx += pos.x - last_pos.x
            dy += pos.y - last_pos.y
            last_pos = pos
            if (dx or dy) and time.time() - last_flush > 0.04:
                while dx or dy or wheel:
                    step_x = max(-127, min(127, dx))
                    step_y = max(-127, min(127, dy))
                    step_w = max(-127, min(127, wheel))
                    api_get(base, f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}", dbg)
                    dx -= step_x
                    dy -= step_y
                    wheel -= step_w
                last_flush = time.time()
            current_left_state = pyautogui.mouseDown(button='left')
            if current_left_state != last_left_state:
                action = "press" if current_left_state else "release"
                transport_manager.send_mouse_command(dx=0, dy=0, wheel=0, button="left", button_action=action)
                last_left_state = current_left_state
            current_right_state = pyautogui.mouseDown(button='right')
            if current_right_state != last_right_state:
                action = "press" if current_right_state else "release"
                transport_manager.send_mouse_command(dx=0, dy=0, wheel=0, button="right", button_action=action)
                last_right_state = current_right_state
            time.sleep(0.005)
    threading.Thread(target=loop, daemon=True).start()
    print("✔ pyautogui fallback (mouse only)")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# SIGNAL HANDLERS
# ═══════════════════════════════════════════════════════════════════════════

sigint_count = 0
sigtstp_count = 0


def handle_sigint(signum, frame):
    """Handle CTRL+C - relay up to 3 times, exit on 4th."""
    global sigint_count
    sigint_count += 1
    if sigint_count >= 4:
        print("\nSIGINT received 4 times - exiting.")
        sys.exit(0)
    print(f"\nSIGINT (CTRL+C) intercepted ({sigint_count}/3) - relaying to target.")
    if transport_manager:
        transport_manager.send_key_command("press", 0x80)  # CTRL
        transport_manager.send_key_command("press", ord('c'))
        time.sleep(0.1)
        transport_manager.send_key_command("release", ord('c'))
        transport_manager.send_key_command("release", 0x80)


def handle_sigtstp(signum, frame):
    """Handle CTRL+Z - relay up to 3 times, exit on 4th."""
    global sigtstp_count
    sigtstp_count += 1
    if sigtstp_count >= 4:
        print("\nSIGTSTP received 4 times - exiting.")
        sys.exit(0)
    print(f"\nSIGTSTP (CTRL+Z) intercepted ({sigtstp_count}/3) - relaying to target.")
    if transport_manager:
        transport_manager.send_key_command("press", 0x80)  # CTRL
        transport_manager.send_key_command("press", ord('z'))
        time.sleep(0.1)
        transport_manager.send_key_command("release", ord('z'))
        transport_manager.send_key_command("release", 0x80)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="HID MQTT Forwarder v4.0 - Phase 1: Critical Fixes + WebSocket"
    )

    # Transport configuration
    ap.add_argument("--transport", choices=["mqtt", "ws", "auto"], default="mqtt",
                    help="Transport mode: mqtt (default), ws (WebSocket), auto (try all)")
    ap.add_argument("--broker", default="broker.emqx.io", help="Single MQTT broker")
    ap.add_argument("--brokers", nargs='+', help="Multiple MQTT brokers")
    ap.add_argument("--ws-port", type=int, default=8765, help="WebSocket server port")

    # Device configuration
    ap.add_argument("--device-id", default="esp32_hid_001", help="Unique device ID")
    ap.add_argument("--debug", action="store_true", help="Debug output")

    # Performance tuning
    ap.add_argument("--sensitivity", type=float, default=0.5, help="Mouse sensitivity (0.1-2.0)")
    ap.add_argument("--rate-limit-ms", type=int, default=20, help="Rate limit in ms (10-200)")

    # Protocol options
    ap.add_argument("--keyboard-state", action="store_true",
                    help="Use state-based keyboard protocol")

    args = ap.parse_args()

    print("🦆 HID Forwarder v4.0 - Phase 1 Starting...")
    print(f"Transport mode: {args.transport}")

    # Create transport manager
    transport_manager = TransportManager(args.device_id)
    transport_manager.use_keyboard_state = args.keyboard_state
    transport_manager.rate_limit_ms = args.rate_limit_ms
    transport_manager.sensitivity = args.sensitivity

    # Setup transports based on mode
    if args.transport in ["mqtt", "auto"]:
        # Setup MQTT transport
        mqtt_brokers = []
        if args.brokers:
            for broker_str in args.brokers:
                if ':' in broker_str:
                    host, port = broker_str.split(':')
                    mqtt_brokers.append((host, int(port)))
                else:
                    mqtt_brokers.append((broker_str, 1883))
        else:
            mqtt_brokers = [(args.broker, 1883)]

        mqtt_transport = MQTTTransport(args.device_id, mqtt_brokers)
        transport_manager.add_transport(mqtt_transport)

    if args.transport in ["ws", "auto"]:
        # Setup WebSocket transport
        ws_transport = WebSocketTransport(args.device_id, port=args.ws_port)
        transport_manager.add_transport(ws_transport)

    # Connect all transports
    transport_manager.connect_all()

    # Setup signal handlers
    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTSTP, handle_sigtstp)

    # Start input capture
    ok = (
        start_evdev("", args.debug)
        or start_pynput("", args.debug)
        or start_pyautogui("", args.debug)
    )

    if not ok:
        print("!! No usable input backend found")
        sys.exit(1)

    # Main loop
    try:
        while True:
            time.sleep(1)
            state = transport_manager.get_connection_state()
            active = transport_manager.get_active_transport_name()
            print(f"[{state.value}] {active}     ", end='\r')
    except KeyboardInterrupt:
        print("\nbye!")
        transport_manager.shutdown()
