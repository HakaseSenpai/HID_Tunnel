#!/usr/bin/env python3
"""
HID Forwarder v5.0 - Phase 2: Out-of-Box Multi-Transport

Focus: Maximum reliability and zero-config operation

New in Phase 2:
- Auto-detect local IP (no manual configuration!)
- HTTP transport (works everywhere, even behind strict firewalls)
- Simple mDNS broadcast (ESP32 can auto-discover host)
- Smart defaults for production use

All Phase 1 features preserved.
"""
from __future__ import annotations
import argparse
import json
import queue
import signal
import socket
import sys
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs

import paho.mqtt.client as mqtt

# ═══════════════════════════════════════════════════════════════════════════
# UTILITIES - AUTO-DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def get_local_ip() -> str:
    """Auto-detect local IP address."""
    try:
        # Create UDP socket to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except:
        return "127.0.0.1"


def broadcast_mdns_simple(device_id: str, ports: Dict[str, int]):
    """Simple mDNS-like UDP broadcast for ESP32 discovery."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        local_ip = get_local_ip()
        announcement = {
            "service": "hid-tunnel",
            "device_id": device_id,
            "host": local_ip,
            "ports": ports
        }

        message = json.dumps(announcement).encode()

        # Broadcast on common discovery port
        def broadcast_loop():
            while True:
                try:
                    sock.sendto(message, ('<broadcast>', 37020))
                    time.sleep(5)  # Announce every 5 seconds
                except:
                    pass

        threading.Thread(target=broadcast_loop, daemon=True).start()
        print(f"[mDNS] Broadcasting on {local_ip} - ports: {ports}")

    except Exception as e:
        print(f"[mDNS] Broadcast failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# CONNECTION STATE & TYPES (from Phase 1)
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionState(Enum):
    NO_TRANSPORTS = "no_transports"
    DISCOVERING = "discovering"
    ACTIVE = "active"
    DEGRADED = "degraded"
    LOCKED = "locked"


@dataclass
class TransportStatus:
    last_seen: float = 0.0
    device_online: bool = False
    last_connect_attempt: float = 0.0
    connect_failures: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# TRANSPORT ABSTRACTION (from Phase 1)
# ═══════════════════════════════════════════════════════════════════════════

class HIDTransport(ABC):
    """Abstract base class for HID transport methods."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self.on_status_callback = None
        self.status = TransportStatus()

    @abstractmethod
    def connect(self) -> bool:
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        pass

    @abstractmethod
    def disconnect(self):
        pass

    @abstractmethod
    def send_mouse(self, command: dict):
        pass

    @abstractmethod
    def send_key(self, command: dict):
        pass

    @abstractmethod
    def send_ping(self, metadata: Optional[dict] = None):
        pass

    @abstractmethod
    def get_transport_name(self) -> str:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# MQTT TRANSPORT (from Phase 1 - unchanged)
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

        self.mouse_topic = f"hid/{device_id}/mouse"
        self.key_topic = f"hid/{device_id}/key"
        self.status_topic = f"hid/{device_id}/status"
        self.ping_topic = f"hid/{device_id}/ping"

        self.reconnect_delays: Dict[str, float] = {}
        self.max_reconnect_delay = 60.0

        self._setup_clients()

    def _setup_clients(self):
        for broker_host, broker_port in self.brokers:
            broker_key = f"{broker_host}:{broker_port}"
            self.broker_statuses[broker_key] = TransportStatus()
            self.reconnect_delays[broker_key] = 1.0

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
            except Exception as e:
                print(f"[MQTT] Failed to create client for {broker_key}: {e}")

    def connect(self) -> bool:
        success = False
        for broker_key, client in self.clients.items():
            if self._connect_broker(broker_key, client):
                success = True
        return success

    def _connect_broker(self, broker_key: str, client: mqtt.Client) -> bool:
        host, port = broker_key.split(':')
        try:
            client.connect(host, int(port), 60)
            client.loop_start()
            return True
        except Exception as e:
            with self.lock:
                self.broker_statuses[broker_key].connect_failures += 1
            return False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        broker_key = userdata
        print(f"[MQTT] ✓ Connected to {broker_key}")
        with self.lock:
            self.reconnect_delays[broker_key] = 1.0
            self.broker_statuses[broker_key].connect_failures = 0
        client.subscribe(self.status_topic, qos=1)
        client.subscribe(self.ping_topic, qos=1)
        self.send_ping(metadata={"broker": broker_key})

    def _on_disconnect(self, client, userdata, rc, properties=None):
        broker_key = userdata
        with self.lock:
            if self.active_broker == broker_key:
                self.active_broker = None
        self._schedule_reconnect(broker_key, client)

    def _schedule_reconnect(self, broker_key: str, client: mqtt.Client):
        def reconnect_worker():
            # Loop instead of recursion to prevent thread exhaustion
            while not client.is_connected():
                delay = self.reconnect_delays.get(broker_key, 1.0)
                time.sleep(delay)

                if client.is_connected():
                    break

                try:
                    client.reconnect()
                    # Reset delay on successful reconnection
                    self.reconnect_delays[broker_key] = 1.0
                    break
                except Exception as e:
                    # Exponential backoff
                    new_delay = min(delay * 2, self.max_reconnect_delay)
                    self.reconnect_delays[broker_key] = new_delay
                    # Loop continues, no new thread created

        threading.Thread(target=reconnect_worker, daemon=True).start()

    def _on_message(self, client, userdata, msg):
        broker_key = userdata
        try:
            payload = json.loads(msg.payload.decode())
            if msg.topic == self.status_topic:
                self._handle_status(broker_key, payload)
        except:
            pass

    def _handle_status(self, broker_key: str, payload: dict):
        if payload.get("status") in ["online", "alive"]:
            with self.lock:
                self.broker_statuses[broker_key].last_seen = time.time()
                self.broker_statuses[broker_key].device_online = True
                self.status.last_seen = time.time()
                if self.active_broker is None:
                    self.active_broker = broker_key
                    if self.on_status_callback:
                        self.on_status_callback(payload)

    def is_connected(self) -> bool:
        with self.lock:
            return self.active_broker is not None

    def disconnect(self):
        for broker_key, client in self.clients.items():
            try:
                client.loop_stop()
                client.disconnect()
            except:
                pass

    def send_mouse(self, command: dict):
        with self.lock:
            if self.active_broker and self.active_broker in self.clients:
                self.clients[self.active_broker].publish(self.mouse_topic, json.dumps(command), qos=0)

    def send_key(self, command: dict):
        with self.lock:
            if self.active_broker and self.active_broker in self.clients:
                self.clients[self.active_broker].publish(self.key_topic, json.dumps(command), qos=1)

    def send_ping(self, metadata: Optional[dict] = None):
        ping_msg = {"from": "host", "device_id": self.device_id, "timestamp": time.time()}
        if metadata:
            ping_msg.update(metadata)
        for broker_key, client in self.clients.items():
            try:
                if client.is_connected():
                    client.publish(self.ping_topic, json.dumps(ping_msg), qos=1)
            except:
                pass

    def get_transport_name(self) -> str:
        with self.lock:
            return f"mqtt://{self.active_broker or '(discovering)'}"


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET TRANSPORT (from Phase 1 - unchanged)
# ═══════════════════════════════════════════════════════════════════════════

class WebSocketTransport(HIDTransport):
    """WebSocket transport - host as server, ESP32 as client."""

    def __init__(self, device_id: str, host: str = "0.0.0.0", port: int = 8765):
        super().__init__(device_id)
        self.host = host
        self.port = port
        self.server = None
        self.client_ws = None
        self.connected = False
        self.lock = threading.Lock()

        try:
            import websockets
            self.websockets = websockets
        except ImportError:
            self.websockets = None
            print("[WS] websockets not available (install: pip install websockets)")

    def connect(self) -> bool:
        if self.websockets is None:
            return False

        try:
            import asyncio

            async def handler(websocket, path):
                print(f"[WS] Client connected from {websocket.remote_address}")
                with self.lock:
                    self.client_ws = websocket
                    self.connected = True
                    self.status.last_seen = time.time()
                try:
                    async for message in websocket:
                        await self._handle_message(message)
                except:
                    pass
                finally:
                    with self.lock:
                        self.client_ws = None
                        self.connected = False

            async def start_server():
                async with self.websockets.serve(handler, self.host, self.port):
                    print(f"[WS] Server on ws://{self.host}:{self.port}")
                    await asyncio.Future()

            def run_server():
                asyncio.run(start_server())

            threading.Thread(target=run_server, daemon=True).start()
            time.sleep(0.5)
            return True
        except Exception as e:
            print(f"[WS] Failed to start: {e}")
            return False

    async def _handle_message(self, message):
        try:
            data = json.loads(message)
            with self.lock:
                self.status.last_seen = time.time()
            if self.on_status_callback and data.get("type") == "status":
                self.on_status_callback(data)
        except:
            pass

    def is_connected(self) -> bool:
        with self.lock:
            return self.connected

    def disconnect(self):
        with self.lock:
            self.client_ws = None
            self.connected = False

    def _send_ws(self, data: dict):
        with self.lock:
            if self.client_ws and self.connected:
                try:
                    import asyncio
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    asyncio.ensure_future(self.client_ws.send(json.dumps(data)))
                except:
                    pass

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
# HTTP TRANSPORT (Phase 2: NEW! Maximum compatibility fallback)
# ═══════════════════════════════════════════════════════════════════════════

class HTTPTransport(HIDTransport):
    """HTTP transport - long-polling for maximum compatibility."""

    def __init__(self, device_id: str, host: str = "0.0.0.0", port: int = 8080):
        super().__init__(device_id)
        self.host = host
        self.port = port
        self.server = None
        self.connected = False
        self.lock = threading.Lock()
        # Limit queue size to prevent memory exhaustion (max 100 pending commands)
        self.pending_commands = queue.Queue(maxsize=100)
        self.last_poll_time = 0

    def connect(self) -> bool:
        try:
            handler = self._create_handler()
            self.server = HTTPServer((self.host, self.port), handler)

            def run_server():
                print(f"[HTTP] Server on http://{self.host}:{self.port}")
                self.server.serve_forever()

            threading.Thread(target=run_server, daemon=True).start()
            time.sleep(0.3)
            self.connected = True
            return True
        except Exception as e:
            print(f"[HTTP] Failed to start: {e}")
            return False

    def _create_handler(self):
        transport = self

        class HIDHTTPHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Suppress logging

            def do_GET(self):
                """Long polling for commands from device."""
                if self.path.startswith("/poll"):
                    transport.last_poll_time = time.time()
                    try:
                        # Wait up to 25 seconds for a command
                        try:
                            command = transport.pending_commands.get(timeout=25)
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()
                            self.wfile.write(json.dumps(command).encode())
                        except queue.Empty:
                            # Timeout - send heartbeat
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.send_header("Access-Control-Allow-Origin", "*")
                            self.end_headers()
                            self.wfile.write(json.dumps({"type": "heartbeat"}).encode())
                    except:
                        self.send_error(500)
                else:
                    self.send_error(404)

            def do_POST(self):
                """Status updates from device."""
                if self.path.startswith("/status"):
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                        body = self.rfile.read(length)
                        data = json.loads(body.decode())

                        with transport.lock:
                            transport.status.last_seen = time.time()
                            transport.connected = True

                        if transport.on_status_callback:
                            transport.on_status_callback(data)

                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(json.dumps({"ok": True}).encode())
                    except:
                        self.send_error(500)
                else:
                    self.send_error(404)

        return HIDHTTPHandler

    def is_connected(self) -> bool:
        with self.lock:
            # Consider connected if device polled recently (within 35 seconds)
            return self.connected and (time.time() - self.last_poll_time < 35)

    def disconnect(self):
        if self.server:
            self.server.shutdown()
        self.connected = False

    def send_mouse(self, command: dict):
        command["type"] = "mouse"
        try:
            # Use timeout to prevent blocking if queue is full
            self.pending_commands.put(command, timeout=0.1)
        except queue.Full:
            # Drop command if queue is full (prevents memory exhaustion)
            pass

    def send_key(self, command: dict):
        command["type"] = "key"
        try:
            self.pending_commands.put(command, timeout=0.1)
        except queue.Full:
            pass

    def send_ping(self, metadata: Optional[dict] = None):
        ping = {"type": "ping", "device_id": self.device_id, "timestamp": time.time()}
        if metadata:
            ping.update(metadata)
        try:
            self.pending_commands.put(ping, timeout=0.1)
        except queue.Full:
            pass

    def get_transport_name(self) -> str:
        return f"http://{self.host}:{self.port}"


# ═══════════════════════════════════════════════════════════════════════════
# TRANSPORT MANAGER (Enhanced for Phase 2 with auto-selection)
# ═══════════════════════════════════════════════════════════════════════════

class TransportManager:
    """Manages multiple transports with smart auto-selection."""

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
        transport.on_status_callback = self._on_transport_status
        self.transports.append(transport)
        print(f"[TRANSPORT] Added: {transport.get_transport_name()}")

    def connect_all(self):
        if not self.transports:
            self.connection_state = ConnectionState.NO_TRANSPORTS
            print("[TRANSPORT] ⚠ No transports configured!")
            return

        self.connection_state = ConnectionState.DISCOVERING
        print(f"[TRANSPORT] Connecting {len(self.transports)} transport(s)...")

        for transport in self.transports:
            try:
                transport.connect()
                time.sleep(0.3)
            except Exception as e:
                print(f"[TRANSPORT] Failed: {transport.get_transport_name()}: {e}")

    def _on_transport_status(self, status: dict):
        """First transport to respond wins."""
        with self.lock:
            if self.active_transport is None:
                for transport in self.transports:
                    if transport.is_connected():
                        self.active_transport = transport
                        self.connection_state = ConnectionState.ACTIVE
                        print(f"[TRANSPORT] ✓✓ Active: {transport.get_transport_name()}")
                        self.send_key_command("release_all", 0)
                        break

    def _timeout_handler(self):
        inactivity_timeout_s = 2
        while True:
            now = time.time()
            if now - self.last_key_time > inactivity_timeout_s:
                self.send_key_command("release_all", 0)
                self.last_key_time = now
            time.sleep(0.5)

    def _discovery_handler(self):
        while True:
            time.sleep(3)
            for transport in self.transports:
                try:
                    if transport.is_connected():
                        transport.send_ping()
                except:
                    pass

            with self.lock:
                if self.active_transport and not self.active_transport.is_connected():
                    print(f"[TRANSPORT] Lost: {self.active_transport.get_transport_name()}")
                    self.active_transport = None
                    self.connection_state = ConnectionState.DISCOVERING

    def _should_send(self) -> bool:
        now = time.time()
        if now - self.last_send_time >= self.rate_limit_ms / 1000.0:
            self.last_send_time = now
            return True
        return False

    def _smooth_and_scale(self, dx, dy):
        self.smoothed_dx = self.alpha * dx + (1 - self.alpha) * self.smoothed_dx
        self.smoothed_dy = self.alpha * dy + (1 - self.alpha) * self.smoothed_dy
        return int(self.smoothed_dx * self.sensitivity), int(self.smoothed_dy * self.sensitivity)

    def send_mouse_command(self, dx=0, dy=0, wheel=0, button=None, button_action=None):
        with self.lock:
            if not self.active_transport:
                return

        force = bool(button and button_action)
        if not force and not self._should_send():
            if dx or dy or wheel:
                with self.mouse_lock:
                    self.pending_mouse_dx += dx
                    self.pending_mouse_dy += dy
                    self.pending_mouse_wheel += wheel
            return

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
        with self.lock:
            if not self.active_transport:
                return

        if self.use_keyboard_state:
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
        with self.lock:
            return self.connection_state

    def get_active_transport_name(self) -> str:
        with self.lock:
            if self.active_transport:
                return self.active_transport.get_transport_name()
            return f"[{self.connection_state.value}]"

    def shutdown(self):
        print("[TRANSPORT] Shutting down...")
        for transport in self.transports:
            try:
                transport.disconnect()
            except:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCE (for backends)
# ═══════════════════════════════════════════════════════════════════════════

transport_manager: Optional[TransportManager] = None


def api_get(base: str, path: str, dbg: bool, timeout: float = 1.5) -> bytes:
    """Legacy API for input backends."""
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
    except:
        pass

    return b"OK"


# ═══════════════════════════════════════════════════════════════════════════
# KEY CODE LOOKUP TABLES (unchanged)
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
# INPUT BACKENDS (unchanged from v4)
# ═══════════════════════════════════════════════════════════════════════════

def start_evdev(base: str, dbg: bool) -> bool:
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
        except:
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
    try:
        from pynput import mouse, keyboard
    except:
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
    try:
        import pyautogui
    except:
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
    global sigint_count
    sigint_count += 1
    if sigint_count >= 4:
        print("\nSIGINT received 4 times - exiting.")
        sys.exit(0)
    print(f"\nSIGINT (CTRL+C) intercepted ({sigint_count}/3) - relaying to target.")
    if transport_manager:
        transport_manager.send_key_command("press", 0x80)
        transport_manager.send_key_command("press", ord('c'))
        time.sleep(0.1)
        transport_manager.send_key_command("release", ord('c'))
        transport_manager.send_key_command("release", 0x80)


def handle_sigtstp(signum, frame):
    global sigtstp_count
    sigtstp_count += 1
    if sigtstp_count >= 4:
        print("\nSIGTSTP received 4 times - exiting.")
        sys.exit(0)
    print(f"\nSIGTSTP (CTRL+Z) intercepted ({sigtstp_count}/3) - relaying to target.")
    if transport_manager:
        transport_manager.send_key_command("press", 0x80)
        transport_manager.send_key_command("press", ord('z'))
        time.sleep(0.1)
        transport_manager.send_key_command("release", ord('z'))
        transport_manager.send_key_command("release", 0x80)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="HID Forwarder v5.0 - Phase 2: Out-of-Box Multi-Transport"
    )

    # Transport mode
    ap.add_argument("--transport", choices=["mqtt", "ws", "http", "auto"], default="auto",
                    help="Transport: mqtt, ws, http, or auto (default: auto tries all)")

    # MQTT options
    ap.add_argument("--broker", default="broker.emqx.io", help="Single MQTT broker")
    ap.add_argument("--brokers", nargs='+', help="Multiple MQTT brokers")

    # WebSocket options
    ap.add_argument("--ws-host", default="0.0.0.0", help="WebSocket host (default: 0.0.0.0)")
    ap.add_argument("--ws-port", type=int, default=8765, help="WebSocket port (default: 8765)")

    # HTTP options
    ap.add_argument("--http-host", default="0.0.0.0", help="HTTP host (default: 0.0.0.0)")
    ap.add_argument("--http-port", type=int, default=8080, help="HTTP port (default: 8080)")

    # Phase 2: Auto-discovery
    ap.add_argument("--enable-mdns", action="store_true",
                    help="Enable mDNS broadcast for ESP32 auto-discovery")

    # Device configuration
    ap.add_argument("--device-id", default="esp32_hid_001", help="Unique device ID")
    ap.add_argument("--debug", action="store_true", help="Debug output")

    # Performance tuning
    ap.add_argument("--sensitivity", type=float, default=0.5, help="Mouse sensitivity")
    ap.add_argument("--rate-limit-ms", type=int, default=20, help="Rate limit in ms")

    # Protocol options
    ap.add_argument("--keyboard-state", action="store_true", help="Use state-based keyboard")

    args = ap.parse_args()

    print("🦆 HID Forwarder v5.0 - Phase 2 Starting...")
    print(f"Transport mode: {args.transport}")

    # Auto-detect local IP
    local_ip = get_local_ip()
    print(f"[AUTO] Local IP: {local_ip}")

    # Create transport manager
    transport_manager = TransportManager(args.device_id)
    transport_manager.use_keyboard_state = args.keyboard_state
    transport_manager.rate_limit_ms = args.rate_limit_ms
    transport_manager.sensitivity = args.sensitivity

    # Track ports for mDNS
    service_ports = {}

    # Setup transports based on mode
    if args.transport in ["mqtt", "auto"]:
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
        print(f"[MQTT] Configured {len(mqtt_brokers)} broker(s)")

    if args.transport in ["ws", "auto"]:
        ws_transport = WebSocketTransport(args.device_id, host=args.ws_host, port=args.ws_port)
        transport_manager.add_transport(ws_transport)
        service_ports["ws"] = args.ws_port
        print(f"[WS] Configured on {local_ip}:{args.ws_port}")

    if args.transport in ["http", "auto"]:
        http_transport = HTTPTransport(args.device_id, host=args.http_host, port=args.http_port)
        transport_manager.add_transport(http_transport)
        service_ports["http"] = args.http_port
        print(f"[HTTP] Configured on {local_ip}:{args.http_port}")

    # Enable mDNS broadcast if requested
    if args.enable_mdns and service_ports:
        broadcast_mdns_simple(args.device_id, service_ports)

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

    # Display connection info
    print("\n" + "="*60)
    print("📡 READY FOR ESP32 CONNECTION")
    print("="*60)
    print(f"Device ID: {args.device_id}")
    print(f"Local IP: {local_ip}")
    if "ws" in service_ports:
        print(f"WebSocket: ws://{local_ip}:{service_ports['ws']}")
    if "http" in service_ports:
        print(f"HTTP: http://{local_ip}:{service_ports['http']}")
    if args.enable_mdns:
        print(f"mDNS: Broadcasting (ESP32 will auto-discover)")
    print("\nESP32 Configuration:")
    if "ws" in service_ports:
        print(f'  WS_ENDPOINTS[] = {{"{local_ip}", {service_ports["ws"]}, "/"}};')
    if "http" in service_ports:
        print(f'  HTTP_ENDPOINTS[] = {{"{local_ip}", {service_ports["http"]}}};')
    print("="*60 + "\n")

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
