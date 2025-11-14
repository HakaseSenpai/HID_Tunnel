#!/usr/bin/env python3
"""
MQTT HID Forwarder - Forwards HID events over MQTT
Based on HID_remote.py but sends data via MQTT instead of direct HTTP
Updated with timeout, rate limiting, smoothing, and sensitivity scaling.
Enhanced to force-send button actions for reliable clicks.
"""
from __future__ import annotations
import argparse, os, queue, sys, threading, time, urllib.request, urllib.error
import json
import paho.mqtt.client as mqtt
import signal  # New: For signal handling

class MQTTHIDForwarder:
    def __init__(self, mqtt_brokers=None, device_id="esp32_hid_001",
                 sensitivity=0.5, rate_limit_ms=20, inactivity_timeout_s=2, global_timeout_s=5,
                 click_hold_ms=50, use_keyboard_state=False):
        # Multi-broker support
        if mqtt_brokers is None:
            mqtt_brokers = [("broker.emqx.io", 1883)]
        self.mqtt_brokers = mqtt_brokers  # List of (host, port) tuples
        self.device_id = device_id
        self.clients = {}  # Dictionary of broker -> client
        self.active_broker = None  # Currently active broker for HID commands
        self.broker_statuses = {}  # Track broker health
        self.command_queue = queue.Queue()

        # Configurable features - improved defaults for lower latency
        self.sensitivity = max(0.1, min(2.0, sensitivity))
        self.rate_limit_ms = max(10, min(200, rate_limit_ms))  # Default now 20ms (50Hz)
        self.inactivity_timeout_s = inactivity_timeout_s
        self.global_timeout_s = global_timeout_s
        self.click_hold_ms = click_hold_ms

        # Timeout and smoothing state
        self.last_activity_time = time.time()
        self.last_key_time = time.time()
        self.last_send_time = time.time()
        self.smoothed_dx = 0.0  # For EMA smoothing
        self.smoothed_dy = 0.0
        self.alpha = 0.5  # EMA smoothing factor

        # Signal handling counters
        self.sigint_count = 0  # CTRL+C
        self.sigtstp_count = 0  # CTRL+Z

        # State-based keyboard protocol
        self.use_keyboard_state = use_keyboard_state
        self.currently_pressed = set()  # Set of currently pressed HID codes

        # MQTT topics
        self.mouse_topic = f"hid/{device_id}/mouse"
        self.key_topic = f"hid/{device_id}/key"
        self.status_topic = f"hid/{device_id}/status"
        self.ping_topic = f"hid/{device_id}/ping"

        # Latency optimization: mouse movement queue for dropping stale events
        self.pending_mouse_dx = 0
        self.pending_mouse_dy = 0
        self.pending_mouse_wheel = 0
        self.mouse_lock = threading.Lock()

        self.setup_mqtt()
        # Start background threads
        threading.Thread(target=self._timeout_handler, daemon=True).start()
        threading.Thread(target=self._discovery_handler, daemon=True).start()

    def setup_mqtt(self):
        """Setup MQTT clients for all configured brokers with multi-broker discovery."""
        print(f"Setting up MQTT connections to {len(self.mqtt_brokers)} broker(s)...")

        for broker_host, broker_port in self.mqtt_brokers:
            broker_key = f"{broker_host}:{broker_port}"
            self.broker_statuses[broker_key] = {"last_seen": 0, "device_online": False}

            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{self.device_id}_host_{broker_key}")
                client.user_data_set(broker_key)  # Store broker key in user data
                client.on_connect = self.on_connect
                client.on_disconnect = self.on_disconnect
                client.on_message = self.on_message

                # Retry connection with exponential backoff
                max_retries = 3
                retry_delay = 1

                for attempt in range(max_retries):
                    try:
                        print(f"Connecting to {broker_host}:{broker_port}... (attempt {attempt + 1})")
                        client.connect(broker_host, broker_port, 60)
                        client.loop_start()
                        self.clients[broker_key] = client
                        print(f"✓ Connection initiated to {broker_key}")
                        break
                    except Exception as e:
                        print(f"Connection to {broker_key} failed: {e}")
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay)
                            retry_delay *= 2
                        else:
                            print(f"Max retries reached for {broker_key}")
            except Exception as e:
                print(f"Failed to setup client for {broker_key}: {e}")

    def on_connect(self, client, userdata, flags, rc, properties=None):
        """Called when a broker connection is established. Subscribe to status topic for discovery."""
        broker_key = userdata
        print(f"✔ Connected to {broker_key} (rc={rc})")

        # Subscribe to status and ping topics for device discovery
        client.subscribe(self.status_topic, qos=1)
        client.subscribe(self.ping_topic, qos=1)

        # Send ping to discover device on this broker
        ping_msg = json.dumps({
            "from": "host",
            "device_id": self.device_id,
            "timestamp": time.time()
        })
        client.publish(self.ping_topic, ping_msg, qos=1)
        print(f"Sent discovery ping on {broker_key}")

    def on_disconnect(self, client, userdata, rc, properties=None):
        """Called when disconnected from a broker."""
        broker_key = userdata
        print(f"✗ Disconnected from {broker_key} (rc={rc})")

        # If this was the active broker, clear it and trigger rediscovery
        if self.active_broker == broker_key:
            print(f"Lost active broker {broker_key}, entering discovery mode...")
            self.active_broker = None

    def on_message(self, client, userdata, msg):
        """Handle incoming messages from brokers (mainly status responses)."""
        broker_key = userdata
        try:
            payload = json.loads(msg.payload.decode())

            if msg.topic == self.status_topic:
                # Device sent status response
                if payload.get("status") in ["online", "alive"]:
                    self.broker_statuses[broker_key]["last_seen"] = time.time()
                    self.broker_statuses[broker_key]["device_online"] = True

                    # If we don't have an active broker, or this is a better one, switch to it
                    if self.active_broker is None:
                        self.active_broker = broker_key
                        print(f"✓✓ Device discovered on {broker_key} - now active HID broker")

                        # Send release_all to ensure clean state after reconnect
                        self.send_key_command("release_all", 0)
        except Exception as e:
            print(f"Error processing message from {broker_key}: {e}")

    def _discovery_handler(self):
        """Background thread: Periodic rediscovery and health monitoring."""
        while True:
            time.sleep(3)  # Check every 3 seconds

            # Send periodic pings to all brokers to keep discovering the device
            for broker_key, client in self.clients.items():
                try:
                    if client.is_connected():
                        ping_msg = json.dumps({
                            "from": "host",
                            "device_id": self.device_id,
                            "timestamp": time.time()
                        })
                        client.publish(self.ping_topic, ping_msg, qos=1)
                except Exception as e:
                    print(f"Error sending ping to {broker_key}: {e}")

            # Check if active broker has timed out
            if self.active_broker:
                last_seen = self.broker_statuses.get(self.active_broker, {}).get("last_seen", 0)
                if time.time() - last_seen > 10:  # 10 second timeout
                    print(f"Active broker {self.active_broker} timed out, entering discovery mode...")
                    self.active_broker = None

    def _timeout_handler(self):
        """Background thread: Check for inactivity and send release_all or flush mouse."""
        while True:
            now = time.time()
            if now - self.last_key_time > self.inactivity_timeout_s:
                self.send_key_command("release_all", 0)
                self.last_key_time = now  # Prevent spamming
            if now - self.last_activity_time > self.global_timeout_s:
                self._flush_mouse(force=True)
            time.sleep(0.5)  # Check every 500ms

    def _smooth_and_scale(self, dx, dy):
        """Apply EMA smoothing and sensitivity scaling. (unchanged)"""
        self.smoothed_dx = self.alpha * dx + (1 - self.alpha) * self.smoothed_dx
        self.smoothed_dy = self.alpha * dy + (1 - self.alpha) * self.smoothed_dy
        return int(self.smoothed_dx * self.sensitivity), int(self.smoothed_dy * self.sensitivity)

    def _should_send(self):
        """Rate limiting: True if enough time has passed since last send. (unchanged)"""
        now = time.time()
        if now - self.last_send_time >= self.rate_limit_ms / 1000.0:
            self.last_send_time = now
            return True
        return False

    def _flush_mouse(self, dx=0, dy=0, wheel=0, button=None, button_action=None, force=False):
        """Aggregate and send mouse command with rate limiting and latency optimization.
        Force-send if button action is present to ensure clicks are reliable.
        Uses pending values to drop stale movements when lagging."""
        if button and button_action:
            force = True  # Bypass rate limit for clicks
        if not force and not self._should_send():
            # For movements, accumulate in pending instead of dropping
            if dx or dy or wheel:
                with self.mouse_lock:
                    self.pending_mouse_dx += dx
                    self.pending_mouse_dy += dy
                    self.pending_mouse_wheel += wheel
            return  # Rate limit: Skip if too soon

        # Get latest accumulated movement
        with self.mouse_lock:
            if dx or dy or wheel:
                self.pending_mouse_dx += dx
                self.pending_mouse_dy += dy
                self.pending_mouse_wheel += wheel
            final_dx = self.pending_mouse_dx
            final_dy = self.pending_mouse_dy
            final_wheel = self.pending_mouse_wheel
            # Clear pending
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
            command["button"] = button  # e.g., "left", "right", "middle"
            command["button_action"] = button_action  # "press", "release", "release_all"

        # Use QoS 0 for mouse movements (low latency), but only if we have an active broker
        if self.active_broker and self.active_broker in self.clients:
            client = self.clients[self.active_broker]
            client.publish(self.mouse_topic, json.dumps(command), qos=0)
            self.last_activity_time = time.time()
        elif not self.active_broker:
            pass  # Silently drop if no active broker (avoid spam)

    def send_mouse_command(self, dx=0, dy=0, wheel=0, button=None, button_action=None):
        """Send mouse with smoothing, scaling, rate limiting, and optional button action."""
        self._flush_mouse(dx, dy, wheel, button, button_action)
        self.last_activity_time = time.time()

    def send_key_command(self, action, key_code):
        """Send key command with state tracking and QoS 1 for reliability.
        Supports both legacy event-based and new state-based protocols."""
        if not self.active_broker or self.active_broker not in self.clients:
            return  # No active broker, silently drop

        client = self.clients[self.active_broker]

        if self.use_keyboard_state:
            # State-based protocol: update currently_pressed set
            if action == "press":
                self.currently_pressed.add(key_code)
            elif action == "release":
                self.currently_pressed.discard(key_code)
            elif action == "release_all":
                self.currently_pressed.clear()

            # Send state update
            command = {
                "action": "state",
                "pressed": list(self.currently_pressed),
                "timestamp": time.time()
            }
        else:
            # Legacy event-based protocol
            command = {
                "action": action,  # "press" or "release" or "release_all"
                "key": key_code,
                "timestamp": time.time()
            }

        # Use QoS 1 for keyboard events (reliability)
        client.publish(self.key_topic, json.dumps(command), qos=1)
        self.last_activity_time = time.time()
        self.last_key_time = time.time()  # Specific to keys

    # New: Signal handlers (unchanged)
    def handle_sigint(self, signum, frame):
        """Handle CTRL+C (SIGINT) - relay up to 3 times, exit on 4th."""
        self.sigint_count += 1
        if self.sigint_count >= 4:
            print("SIGINT received 4 times - exiting.")
            sys.exit(0)
        print(f"SIGINT (CTRL+C) intercepted ({self.sigint_count}/3) - relaying to target.")
        # Relay CTRL + C
        self.send_key_command("press", 0x80)  # CTRL (HID code)
        self.send_key_command("press", ord('c'))  # C
        time.sleep(0.1)  # Brief hold
        self.send_key_command("release", ord('c'))
        self.send_key_command("release", 0x80)

    def handle_sigtstp(self, signum, frame):
        """Handle CTRL+Z (SIGTSTP) - relay up to 3 times, exit on 4th."""
        self.sigtstp_count += 1
        if self.sigtstp_count >= 4:
            print("SIGTSTP received 4 times - exiting.")
            sys.exit(0)
        print(f"SIGTSTP (CTRL+Z) intercepted ({self.sigtstp_count}/3) - relaying to target.")
        # Relay CTRL + Z
        self.send_key_command("press", 0x80)  # CTRL
        self.send_key_command("press", ord('z'))  # Z
        time.sleep(0.1)
        self.send_key_command("release", ord('z'))
        self.send_key_command("release", 0x80)

# Modified API functions to use MQTT (integrated with new features)
mqtt_forwarder = None

def api_get(base: str, path: str, dbg: bool, timeout: float = 1.5) -> bytes:
    """Modified to send via MQTT instead of HTTP"""
    global mqtt_forwarder

    if not mqtt_forwarder:
        return b""

    try:
        if "/mouse?" in path:
            params = {}
            # Existing parses...
            if "dx=" in path: params["dx"] = int(path.split("dx=")[1].split("&")[0])
            if "dy=" in path: params["dy"] = int(path.split("dy=")[1].split("&")[0])
            if "wheel=" in path: params["wheel"] = int(path.split("wheel=")[1].split("&")[0])
            # NEW: Parse button and action
            button = None
            button_action = None
            if "button=" in path: button = path.split("button=")[1].split("&")[0]
            if "button_action=" in path: button_action = path.split("button_action=")[1].split("&")[0] or path.split("button_action=")[1]  # Handle end of string

            mqtt_forwarder.send_mouse_command(
                params.get("dx", 0),
                params.get("dy", 0),
                params.get("wheel", 0),
                button=button,  # Pass if present
                button_action=button_action
            )

        elif "/key?" in path:
            # Parse key parameters
            if "press=" in path:
                key_code = int(path.split("press=")[1].split("&")[0])
                mqtt_forwarder.send_key_command("press", key_code)
            elif "release=" in path:
                key_code = int(path.split("release=")[1].split("&")[0])
                mqtt_forwarder.send_key_command("release", key_code)
            else:
                mqtt_forwarder.send_key_command("release_all", 0)

        if dbg:
            print(f"→ MQTT: {path}")

    except Exception as e:
        if dbg:
            print(f"[MQTT] {e} ← {path}")

    return b"OK"

# ————
# Key-code lookup tables (unchanged)
# ————
EV2HID: dict[int, int] = {    # Linux evdev → Arduino HID
    1: 0xB1,  # Esc
    # number row
    2: ord('1'),  3: ord('2'),  4: ord('3'),  5: ord('4'),  6: ord('5'),
    7: ord('6'),  8: ord('7'),  9: ord('8'), 10: ord('9'), 11: ord('0'),
    12: ord('-'), 13: ord('='),
    # modifiers & editing
    14: 0xB2,  15: 0xB3, 28: 0xB0,
    29: 0x80,  42: 0x81, 54: 0x85,
    56: 0x82,  57: ord(' '),
    # alpha
    16: ord('q'), 17: ord('w'), 18: ord('e'), 19: ord('r'), 20: ord('t'),
    21: ord('y'), 22: ord('u'), 23: ord('i'), 24: ord('o'), 25: ord('p'),
    26: ord('['), 27: ord(']'),
    30: ord('a'), 31: ord('s'), 32: ord('d'), 33: ord('f'), 34: ord('g'),
    35: ord('h'), 36: ord('j'), 37: ord('k'), 38: ord('l'),
    39: ord(';'), 40: ord("'"), 41: ord('`'), 43: ord('\\'),
    44: ord('z'), 45: ord('x'), 46: ord('c'), 47: ord('v'), 48: ord('b'),
    49: ord('n'), 50: ord('m'),
    51: ord(','), 52: ord('.'), 53: ord('/'),
    # arrows
   105: 0xD8, 106: 0xD7, 103: 0xDA, 108: 0xD9,
   111: 0xD4,
    # F-keys
    59: 0xC2, 60: 0xC3, 61: 0xC4, 62: 0xC5, 63: 0xC6,
    64: 0xC7, 65: 0xC8, 66: 0xC9, 67: 0xCA, 68: 0xCB,
    87: 0xCC, 88: 0xCD,
}

def vk2hid(vk: int) -> int | None:    # Windows VK / XKB → HID
    if 0x30 <= vk <= 0x39:    # 0-9
        return vk
    if 0x41 <= vk <= 0x5A:    # A-Z → a-z
        return vk + 32
    if vk in (0x25, 0x27, 0x26, 0x28):    # arrows
        return {0x25:0xD8, 0x27:0xD7, 0x26:0xDA, 0x28:0xD9}[vk]
    if 0x70 <= vk <= 0x7B:    # F1-F12
        return 0xC2 + (vk - 0x70)
    if vk == 0x2E:    # Delete
        return 0xD4
    return EV2HID.get(vk)

# ————
# Backend #1 – evdev  (Linux) - Integrated with new send_mouse_command
# ————
def start_evdev(base: str, dbg: bool) -> bool:
    try:
        from evdev import InputDevice, categorize, ecodes, list_devices  # type: ignore
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
                    # New: Detect mouse buttons (evdev codes for left/right/middle)
                    if ev.code == ecodes.BTN_LEFT:
                        button = "left"
                        action = "press" if ev.value == 1 else "release"
                        api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
                        if dbg:
                            print(f"evdev: Detected {button} {action}")
                    elif ev.code == ecodes.BTN_RIGHT:
                        button = "right"
                        action = "press" if ev.value == 1 else "release"
                        api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
                        if dbg:
                            print(f"evdev: Detected {button} {action}")
                    elif ev.code == ecodes.BTN_MIDDLE:
                        button = "middle"
                        action = "press" if ev.value == 1 else "release"
                        api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
                        if dbg:
                            print(f"evdev: Detected {button} {action}")
                    else:
                        # Existing key handling
                        hid = EV2HID.get(ev.code)
                        if hid:
                            api_get(base, f"/key?{'press' if ev.value else 'release'}={hid}", dbg)
            if (dx or dy or wheel) and time.time() - last_flush > 0.04:
                # ── send in USB-legal chunks (-127 … +127) ────
                while dx or dy or wheel:
                    step_x = max(-127, min(127, dx))
                    step_y = max(-127, min(127, dy))
                    step_w = max(-127, min(127, wheel))
                    api_get(base,
                            f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
                            dbg)
                    dx    -= step_x
                    dy    -= step_y
                    wheel -= step_w
                last_flush = time.time()
    threading.Thread(target=mixer, daemon=True).start()
    return True

# ————
# Backend #2 – pynput  (X11 / Wayland / Windows) - Integrated with new send_mouse_command
# ————
def start_pynput(base: str, dbg: bool) -> bool:
    try:
        from pynput import mouse, keyboard    # type: ignore
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
            api_get(base,
                    f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
                    dbg)
            dx    -= step_x
            dy    -= step_y
            wheel -= step_w
        last_flush = time.time()

    # mouse callbacks ----
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

    # New: Mouse click callback with debug
    def on_click(x, y, button, pressed):
        button_str = {mouse.Button.left: "left", mouse.Button.right: "right", mouse.Button.middle: "middle"}.get(button)
        if button_str:
            action = "press" if pressed else "release"
            mqtt_forwarder.send_mouse_command(dx=0, dy=0, wheel=0, button=button_str, button_action=action)
            if dbg:
                print(f"pynput: Detected {button_str} {action}")

    mouse.Listener(on_move=on_move, on_scroll=on_scroll, on_click=on_click).start()

    # keyboard callbacks ----
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

# ————
# Backend #3 – pyautogui  (mouse only, no keys – fallback) - Integrated with new send_mouse_command
# ————
def start_pyautogui(base: str, dbg: bool) -> bool:
    try:
        import pyautogui    # type: ignore
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
            # Check left click
            current_left_state = pyautogui.mouseDown(button='left')
            if current_left_state != last_left_state:
                action = "press" if current_left_state else "release"
                mqtt_forwarder.send_mouse_command(dx=0, dy=0, wheel=0, button="left", button_action=action)
                if dbg:
                    print(f"pyautogui: Detected left {action}")
                last_left_state = current_left_state
            # Check right click
            current_right_state = pyautogui.mouseDown(button='right')
            if current_right_state != last_right_state:
                action = "press" if current_right_state else "release"
                mqtt_forwarder.send_mouse_command(dx=0, dy=0, wheel=0, button="right", button_action=action)
                if dbg:
                    print(f"pyautogui: Detected right {action}")
                last_right_state = current_right_state
            time.sleep(0.005)  # Reduced from 0.01s for better click detection
    threading.Thread(target=loop, daemon=True).start()
    print("✔ pyautogui fallback (mouse only)")
    return True

# ————
# main
# ————
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Forward HID input via MQTT with multi-broker support and enhancements")
    # Broker configuration
    ap.add_argument("--broker", default="broker.emqx.io", help="Single MQTT broker address (default: broker.emqx.io)")
    ap.add_argument("--brokers", nargs='+', help="Multiple MQTT brokers (e.g., broker.emqx.io:1883 test.mosquitto.org:1883)")
    ap.add_argument("--device-id", default="esp32_hid_001", help="Unique device ID")
    ap.add_argument("--debug", action="store_true", help="print every MQTT message")
    ap.add_argument("--url", help="Legacy compatibility (ignored in MQTT mode)")
    # Performance tuning (improved defaults for lower latency)
    ap.add_argument("--sensitivity", type=float, default=0.5, help="Mouse speed scaling (0.1-2.0, default 0.5)")
    ap.add_argument("--rate-limit-ms", type=int, default=20, help="Min ms between MQTT sends (10-200, default 20ms for ~50Hz)")
    ap.add_argument("--inactivity-timeout-s", type=int, default=2, help="Seconds of key inactivity before release_all (default 2)")
    ap.add_argument("--global-timeout-s", type=int, default=5, help="Seconds of total inactivity before flush (default 5)")
    ap.add_argument("--click-hold-ms", type=int, default=50, help="ms to hold for clicks (default 50 for natural feel)")
    # State-based keyboard protocol
    ap.add_argument("--keyboard-state", action="store_true", help="Use state-based keyboard protocol (resilient to packet loss)")
    args = ap.parse_args()

    print("🦆 HID-MQTT Forwarder starting...")

    # Parse broker list
    mqtt_brokers = []
    if args.brokers:
        # Multi-broker mode
        for broker_str in args.brokers:
            if ':' in broker_str:
                host, port = broker_str.split(':')
                mqtt_brokers.append((host, int(port)))
            else:
                mqtt_brokers.append((broker_str, 1883))
        print(f"Multi-broker mode: {len(mqtt_brokers)} broker(s)")
    else:
        # Single broker mode
        mqtt_brokers = [(args.broker, 1883)]
        print(f"Single-broker mode: {args.broker}")

    # Initialize MQTT forwarder with new params
    mqtt_forwarder = MQTTHIDForwarder(mqtt_brokers=mqtt_brokers,
                                      device_id=args.device_id,
                                      sensitivity=args.sensitivity,
                                      rate_limit_ms=args.rate_limit_ms,
                                      inactivity_timeout_s=args.inactivity_timeout_s,
                                      global_timeout_s=args.global_timeout_s,
                                      click_hold_ms=args.click_hold_ms,
                                      use_keyboard_state=args.keyboard_state)
    # New: Set up signal handlers
    signal.signal(signal.SIGINT, mqtt_forwarder.handle_sigint)  # CTRL+C
    signal.signal(signal.SIGTSTP, mqtt_forwarder.handle_sigtstp)  # CTRL+Z (Linux/Unix; Windows may need alternative)

    # Start input capture (reuse existing backends)
    ok = (
        start_evdev("", args.debug)  # Empty base URL since we're using MQTT
        or start_pynput("", args.debug)
        or start_pyautogui("", args.debug)
    )

    if not ok:
        print("!! No usable input backend found – install 'python-evdev' or 'pynput' or 'pyautogui'.")
        sys.exit(1)

    try:
        while True:
            time.sleep(1)
            # Print active broker status
            if mqtt_forwarder.active_broker:
                print(f"[Active broker: {mqtt_forwarder.active_broker}]", end='\r')
    except KeyboardInterrupt:
        print("\nbye!")
        # Cleanup all clients
        for broker_key, client in mqtt_forwarder.clients.items():
            try:
                client.loop_stop()
                client.disconnect()
                print(f"Disconnected from {broker_key}")
            except:
                pass


# #!/usr/bin/env python3
# """
# MQTT HID Forwarder - Forwards HID events over MQTT
# Based on HID_remote.py but sends data via MQTT instead of direct HTTP
# Updated with timeout, rate limiting, smoothing, and sensitivity scaling.
# """
# from __future__ import annotations
# import argparse, os, queue, sys, threading, time, urllib.request, urllib.error
# import json
# import paho.mqtt.client as mqtt
# import signal  # New: For signal handling
#
# class MQTTHIDForwarder:
#     def __init__(self, mqtt_broker="broker.emqx.io", mqtt_port=1883, device_id="esp32_hid_001",
#                  sensitivity=0.5, rate_limit_ms=50, inactivity_timeout_s=2, global_timeout_s=5):
#         self.mqtt_broker = mqtt_broker
#         self.mqtt_port = mqtt_port
#         self.device_id = device_id
#         self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)  # Fix deprecation warning
#         self.command_queue = queue.Queue()
#
#         # New: Configurable features
#         self.sensitivity = max(0.1, min(2.0, sensitivity))  # Clamp to reasonable range
#         self.rate_limit_ms = max(10, min(200, rate_limit_ms))  # MQTT send rate (ms between sends)
#         self.inactivity_timeout_s = inactivity_timeout_s  # Timeout for key release_all
#         self.global_timeout_s = global_timeout_s  # Global inactivity flush
#
#         # New: Timeout and smoothing state
#         self.last_activity_time = time.time()
#         self.last_key_time = time.time()
#         self.last_send_time = time.time()
#         self.smoothed_dx = 0.0  # For EMA smoothing
#         self.smoothed_dy = 0.0
#         self.alpha = 0.5  # EMA smoothing factor (0.0-1.0; higher = more smoothing)
#
#         # New: Signal handling counters
#         self.sigint_count = 0  # CTRL+C
#         self.sigtstp_count = 0  # CTRL+Z
#
#         # MQTT topics
#         self.mouse_topic = f"hid/{device_id}/mouse"
#         self.key_topic = f"hid/{device_id}/key"
#         self.status_topic = f"hid/{device_id}/status"
#
#         self.setup_mqtt()
#         # Start background thread for timeouts
#         threading.Thread(target=self._timeout_handler, daemon=True).start()
#
#     def setup_mqtt(self):
#         self.client.on_connect = self.on_connect
#         self.client.on_disconnect = self.on_disconnect
#
#         # Retry connection with exponential backoff
#         max_retries = 5
#         retry_delay = 1
#
#         for attempt in range(max_retries):
#             try:
#                 print(f"Attempting to connect to {self.mqtt_broker}... (attempt {attempt + 1})")
#                 self.client.connect(self.mqtt_broker, self.mqtt_port, 60)
#                 self.client.loop_start()
#                 return
#             except Exception as e:
#                 print(f"Connection failed: {e}")
#                 if attempt < max_retries - 1:
#                     print(f"Retrying in {retry_delay} seconds...")
#                     time.sleep(retry_delay)
#                     retry_delay *= 2  # Exponential backoff
#                 else:
#                     print("Max retries reached. Connection failed.")
#                     raise
#
#     def on_connect(self, client, userdata, flags, rc, properties=None):
#         print(f"✔ Connected to MQTT broker with result code {rc}")
#         # Publish online status
#         client.publish(self.status_topic, json.dumps({"status": "online", "timestamp": time.time()}))
#
#     def on_disconnect(self, client, userdata, rc, properties=None):
#         print(f"✗ Disconnected from MQTT broker with result code {rc}")
#
#     def _timeout_handler(self):
#         """Background thread: Check for inactivity and send release_all or flush mouse. (unchanged)"""
#         while True:
#             now = time.time()
#             if now - self.last_key_time > self.inactivity_timeout_s:
#                 self.send_key_command("release_all", 0)
#                 self.last_key_time = now  # Prevent spamming
#             if now - self.last_activity_time > self.global_timeout_s:
#                 self._flush_mouse(force=True)
#             time.sleep(0.5)  # Check every 500ms
#
#     def _smooth_and_scale(self, dx, dy):
#         """Apply EMA smoothing and sensitivity scaling. (unchanged)"""
#         self.smoothed_dx = self.alpha * dx + (1 - self.alpha) * self.smoothed_dx
#         self.smoothed_dy = self.alpha * dy + (1 - self.alpha) * self.smoothed_dy
#         return int(self.smoothed_dx * self.sensitivity), int(self.smoothed_dy * self.sensitivity)
#
#     def _should_send(self):
#         """Rate limiting: True if enough time has passed since last send. (unchanged)"""
#         now = time.time()
#         if now - self.last_send_time >= self.rate_limit_ms / 1000.0:
#             self.last_send_time = now
#             return True
#         return False
#
#     def _flush_mouse(self, dx=0, dy=0, wheel=0, button=None, button_action=None, force=False):
#         """Aggregate and send mouse command with rate limiting, now including buttons."""
#         if not force and not self._should_send():
#             return  # Rate limit: Skip if too soon
#         scaled_dx, scaled_dy = self._smooth_and_scale(dx, dy)
#         command = {
#             "dx": scaled_dx,
#             "dy": scaled_dy,
#             "wheel": wheel,
#             "timestamp": time.time()
#         }
#         if button and button_action:
#             command["button"] = button  # e.g., "left", "right", "middle"
#             command["button_action"] = button_action  # "press", "release", "release_all"
#         self.client.publish(self.mouse_topic, json.dumps(command))
#         self.last_activity_time = time.time()  # Update activity
#
#     def send_mouse_command(self, dx=0, dy=0, wheel=0, button=None, button_action=None):
#         """Send mouse with smoothing, scaling, rate limiting, and optional button action."""
#         self._flush_mouse(dx, dy, wheel, button, button_action)
#         self.last_activity_time = time.time()
#
#     def send_key_command(self, action, key_code):
#         """Send key command, update key activity time. (unchanged)"""
#         command = {
#             "action": action,  # "press" or "release" or "release_all"
#             "key": key_code,
#             "timestamp": time.time()
#         }
#         self.client.publish(self.key_topic, json.dumps(command))
#         self.last_activity_time = time.time()
#         self.last_key_time = time.time()  # Specific to keys
#
#     # New: Signal handlers
#     def handle_sigint(self, signum, frame):
#         """Handle CTRL+C (SIGINT) - relay up to 3 times, exit on 4th."""
#         self.sigint_count += 1
#         if self.sigint_count >= 4:
#             print("SIGINT received 4 times - exiting.")
#             sys.exit(0)
#         print(f"SIGINT (CTRL+C) intercepted ({self.sigint_count}/3) - relaying to target.")
#         # Relay CTRL + C (with SHIFT if pressed, but signals don't detect SHIFT; assume base for now)
#         self.send_key_command("press", 0x80)  # CTRL (HID code)
#         self.send_key_command("press", ord('c'))  # C
#         time.sleep(0.1)  # Brief hold
#         self.send_key_command("release", ord('c'))
#         self.send_key_command("release", 0x80)
#
#     def handle_sigtstp(self, signum, frame):
#         """Handle CTRL+Z (SIGTSTP) - relay up to 3 times, exit on 4th."""
#         self.sigtstp_count += 1
#         if self.sigtstp_count >= 4:
#             print("SIGTSTP received 4 times - exiting.")
#             sys.exit(0)
#         print(f"SIGTSTP (CTRL+Z) intercepted ({self.sigtstp_count}/3) - relaying to target.")
#         # Relay CTRL + Z
#         self.send_key_command("press", 0x80)  # CTRL
#         self.send_key_command("press", ord('z'))  # Z
#         time.sleep(0.1)
#         self.send_key_command("release", ord('z'))
#         self.send_key_command("release", 0x80)
#
# # Modified API functions to use MQTT (integrated with new features)
# mqtt_forwarder = None
#
# def api_get(base: str, path: str, dbg: bool, timeout: float = 1.5) -> bytes:
#     """Modified to send via MQTT instead of HTTP"""
#     global mqtt_forwarder
#
#     if not mqtt_forwarder:
#         return b""
#
#     try:
#         if "/mouse?" in path:
#             params = {}
#             # Existing parses...
#             if "dx=" in path: params["dx"] = int(path.split("dx=")[1].split("&")[0])
#             if "dy=" in path: params["dy"] = int(path.split("dy=")[1].split("&")[0])
#             if "wheel=" in path: params["wheel"] = int(path.split("wheel=")[1].split("&")[0])
#             # NEW: Parse button and action
#             button = None
#             button_action = None
#             if "button=" in path: button = path.split("button=")[1].split("&")[0]
#             if "button_action=" in path: button_action = path.split("button_action=")[1].split("&")[0] or path.split("button_action=")[1]  # Handle end of string
#
#             mqtt_forwarder.send_mouse_command(
#                 params.get("dx", 0),
#                 params.get("dy", 0),
#                 params.get("wheel", 0),
#                 button=button,  # Pass if present
#                 button_action=button_action
#             )
#
#         elif "/key?" in path:
#             # Parse key parameters
#             if "press=" in path:
#                 key_code = int(path.split("press=")[1].split("&")[0])
#                 mqtt_forwarder.send_key_command("press", key_code)
#             elif "release=" in path:
#                 key_code = int(path.split("release=")[1].split("&")[0])
#                 mqtt_forwarder.send_key_command("release", key_code)
#             else:
#                 mqtt_forwarder.send_key_command("release_all", 0)
#
#         if dbg:
#             print(f"→ MQTT: {path}")
#
#     except Exception as e:
#         if dbg:
#             print(f"[MQTT] {e} ← {path}")
#
#     return b"OK"
#
# # ————
# # Key-code lookup tables (unchanged)
# # ————
# EV2HID: dict[int, int] = {    # Linux evdev → Arduino HID
#     1: 0xB1,  # Esc
#     # number row
#     2: ord('1'),  3: ord('2'),  4: ord('3'),  5: ord('4'),  6: ord('5'),
#     7: ord('6'),  8: ord('7'),  9: ord('8'), 10: ord('9'), 11: ord('0'),
#     12: ord('-'), 13: ord('='),
#     # modifiers & editing
#     14: 0xB2,  15: 0xB3, 28: 0xB0,
#     29: 0x80,  42: 0x81, 54: 0x85,
#     56: 0x82,  57: ord(' '),
#     # alpha
#     16: ord('q'), 17: ord('w'), 18: ord('e'), 19: ord('r'), 20: ord('t'),
#     21: ord('y'), 22: ord('u'), 23: ord('i'), 24: ord('o'), 25: ord('p'),
#     26: ord('['), 27: ord(']'),
#     30: ord('a'), 31: ord('s'), 32: ord('d'), 33: ord('f'), 34: ord('g'),
#     35: ord('h'), 36: ord('j'), 37: ord('k'), 38: ord('l'),
#     39: ord(';'), 40: ord("'"), 41: ord('`'), 43: ord('\\'),
#     44: ord('z'), 45: ord('x'), 46: ord('c'), 47: ord('v'), 48: ord('b'),
#     49: ord('n'), 50: ord('m'),
#     51: ord(','), 52: ord('.'), 53: ord('/'),
#     # arrows
#    105: 0xD8, 106: 0xD7, 103: 0xDA, 108: 0xD9,
#    111: 0xD4,
#     # F-keys
#     59: 0xC2, 60: 0xC3, 61: 0xC4, 62: 0xC5, 63: 0xC6,
#     64: 0xC7, 65: 0xC8, 66: 0xC9, 67: 0xCA, 68: 0xCB,
#     87: 0xCC, 88: 0xCD,
# }
#
# def vk2hid(vk: int) -> int | None:    # Windows VK / XKB → HID
#     if 0x30 <= vk <= 0x39:    # 0-9
#         return vk
#     if 0x41 <= vk <= 0x5A:    # A-Z → a-z
#         return vk + 32
#     if vk in (0x25, 0x27, 0x26, 0x28):    # arrows
#         return {0x25:0xD8, 0x27:0xD7, 0x26:0xDA, 0x28:0xD9}[vk]
#     if 0x70 <= vk <= 0x7B:    # F1-F12
#         return 0xC2 + (vk - 0x70)
#     if vk == 0x2E:    # Delete
#         return 0xD4
#     return EV2HID.get(vk)
#
# # ————
# # Backend #1 – evdev  (Linux) - Integrated with new send_mouse_command
# # ————
# def start_evdev(base: str, dbg: bool) -> bool:
#     try:
#         from evdev import InputDevice, categorize, ecodes, list_devices  # type: ignore
#     except ImportError:
#         return False
#
#     devs: list[InputDevice] = []
#     for path in list_devices():
#         try:
#             d = InputDevice(path)
#             caps = d.capabilities()
#             if ecodes.EV_REL in caps or ecodes.EV_ABS in caps:
#                 devs.append(d)
#         except Exception:
#             pass
#     if not devs:
#         return False
#     print(f"✔ evdev backend – {len(devs)} device(s)")
#
#     q: "queue.SimpleQueue" = queue.SimpleQueue()
#
#     def reader(dev: InputDevice):
#         for ev in dev.read_loop():
#             q.put(ev)
#     for d in devs:
#         threading.Thread(target=reader, args=(d,), daemon=True).start()
#
#     def mixer():
#         dx = dy = wheel = 0
#         last_flush = time.time()
#         last_abs_x = last_abs_y = None
#         while True:
#             try:
#                 ev = q.get(timeout=0.03)
#             except queue.Empty:
#                 pass
#             else:
#                 if ev.type == ecodes.EV_REL:
#                     if ev.code == ecodes.REL_X:    dx += ev.value
#                     elif ev.code == ecodes.REL_Y:    dy += ev.value
#                     elif ev.code == ecodes.REL_WHEEL: wheel += ev.value
#                 elif ev.type == ecodes.EV_ABS:
#                     if ev.code == ecodes.ABS_X:
#                         if last_abs_x is not None:
#                             dx += ev.value - last_abs_x
#                         last_abs_x = ev.value
#                     elif ev.code == ecodes.ABS_Y:
#                         if last_abs_y is not None:
#                             dy += ev.value - last_abs_y
#                         last_abs_y = ev.value
#                 elif ev.type == ecodes.EV_KEY:
#                 # New: Detect mouse buttons (evdev codes for left/right/middle)
#                     if ev.code == ecodes.BTN_LEFT:
#                         button = "left"
#                         action = "press" if ev.value == 1 else "release"
#                         api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
#                     elif ev.code == ecodes.BTN_RIGHT:
#                         button = "right"
#                         action = "press" if ev.value == 1 else "release"
#                         api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
#                     elif ev.code == ecodes.BTN_MIDDLE:
#                         button = "middle"
#                         action = "press" if ev.value == 1 else "release"
#                         api_get(base, f"/mouse?dx=0&dy=0&wheel=0&button={button}&button_action={action}", dbg)
#                     else:
#                         # Existing key handling
#                         hid = EV2HID.get(ev.code)
#                    #hid = EV2HID.get(ev.code)
#                         if hid:
#                             api_get(base, f"/key?{'press' if ev.value else 'release'}={hid}", dbg)
#             if (dx or dy or wheel) and time.time() - last_flush > 0.04:
#                 # ── send in USB-legal chunks (-127 … +127) ────
#                 while dx or dy or wheel:
#                     step_x = max(-127, min(127, dx))
#                     step_y = max(-127, min(127, dy))
#                     step_w = max(-127, min(127, wheel))
#                     api_get(base,
#                             f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
#                             dbg)
#                     dx    -= step_x
#                     dy    -= step_y
#                     wheel -= step_w
#                 last_flush = time.time()
#     threading.Thread(target=mixer, daemon=True).start()
#     return True
#
# # ————
# # Backend #2 – pynput  (X11 / Wayland / Windows) - Integrated with new send_mouse_command
# # ————
# def start_pynput(base: str, dbg: bool) -> bool:
#     try:
#         from pynput import mouse, keyboard    # type: ignore
#     except Exception:
#         return False
#
#     dx = dy = wheel = 0
#     last_flush = time.time()
#
#     def flush():
#         nonlocal dx, dy, wheel, last_flush
#         while dx or dy or wheel:
#             step_x = max(-127, min(127, int(round(dx))))
#             step_y = max(-127, min(127, int(round(dy))))
#             step_w = max(-127, min(127, int(round(wheel))))
#             api_get(base,
#                     f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
#                     dbg)
#             dx    -= step_x
#             dy    -= step_y
#             wheel -= step_w
#         last_flush = time.time()
#
#     # mouse callbacks ----
#     last_xy = [None, None]
#
#     def on_move(x, y):
#         nonlocal dx, dy
#         if last_xy[0] is not None:
#             dx += (x - last_xy[0]) * 0.1
#             dy += (y - last_xy[1]) * 0.1
#         last_xy[:] = [x, y]
#         if time.time() - last_flush > 0.04:
#             flush()
#
#     def on_scroll(_x, _y, _dx, _dy):
#         nonlocal wheel
#         wheel += _dy
#         flush()
#     # New: Mouse click callback
#     def on_click(x, y, button, pressed):
#         button_str = {mouse.Button.left: "left", mouse.Button.right: "right", mouse.Button.middle: "middle"}.get(button)
#         if button_str:
#             action = "press" if pressed else "release"
#             mqtt_forwarder.send_mouse_command(dx=0, dy=0, wheel=0, button=button_str, button_action=action)
#             if dbg:
#                 print(f"Mouse {button_str} {action}")
#
#     mouse.Listener(on_move=on_move, on_scroll=on_scroll, on_click=on_click).start()  # Added on_click
# #    mouse.Listener(on_move=on_move, on_scroll=on_scroll).start()
#
#     # keyboard callbacks ----
#     def on_press(k):
#         vk = getattr(k, "vk", getattr(k, "value", k).vk)
#         hid = vk2hid(vk)
#         if hid:
#             api_get(base, f"/key?press={hid}", dbg)
#
#     def on_release(k):
#         vk = getattr(k, "vk", getattr(k, "value", k).vk)
#         hid = vk2hid(vk)
#         if hid:
#             api_get(base, f"/key?release={hid}", dbg)
#
#     keyboard.Listener(on_press=on_press, on_release=on_release).start()
#     print("✔ pynput backend")
#     return True
#
# # ————
# # Backend #3 – pyautogui  (mouse only, no keys – fallback) - Integrated with new send_mouse_command
# # ————
# def start_pyautogui(base: str, dbg: bool) -> bool:
#     try:
#         import pyautogui    # type: ignore
#     except Exception:
#         return False
#
#     dx = dy = wheel = 0
#     last_flush = time.time()
#     last_pos = pyautogui.position()
#
#     def loop():
#         nonlocal dx, dy, wheel, last_flush, last_pos
#         last_left_state  = pyautogui.mouseDown(button='left')
#         last_right_state = pyautogui.mouseDown(button='right')
#         while True:
#             pos = pyautogui.position()
#             dx += pos.x - last_pos.x
#             dy += pos.y - last_pos.y
#             last_pos = pos
#             if (dx or dy) and time.time() - last_flush > 0.04:
#                 while dx or dy or wheel:
#                     step_x = max(-127, min(127, dx))
#                     step_y = max(-127, min(127, dy))
#                     step_w = max(-127, min(127, wheel))
#                     api_get(base, f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}", dbg)
#                     dx -= step_x
#                     dy -= step_y
#                     wheel -= step_w
#                 last_flush = time.time()
#             # Check left click
#             current_left_state = pyautogui.mouseDown(button='left')
#             if current_left_state != last_left_state:
#                 action = "press" if current_left_state else "release"
#                 mqtt_forwarder.send_mouse_command(dx=0, dy=0, wheel=0, button="left", button_action=action)
#                 last_left_state = current_left_state
#             # Check right click
#             current_right_state = pyautogui.mouseDown(button='right')
#             if current_right_state != last_right_state:
#                 action = "press" if current_right_state else "release"
#                 mqtt_forwarder.send_mouse_command(dx=0, dy=0, wheel=0, button="right", button_action=action)
#                 last_right_state = current_left_state
#             time.sleep(0.01)
#     threading.Thread(target=loop, daemon=True).start()
#     print("✔ pyautogui fallback (mouse only)")
#     return True
#
# # ————
# # main
# # ————
# if __name__ == "__main__":
#     ap = argparse.ArgumentParser(description="Forward HID input via MQTT with enhancements")
#     ap.add_argument("--broker", default="broker.emqx.io", help="MQTT broker address")
#     ap.add_argument("--device-id", default="esp32_hid_001", help="Unique device ID")
#     ap.add_argument("--debug", action="store_true", help="print every MQTT message")
#     ap.add_argument("--url", help="Legacy compatibility (ignored in MQTT mode)")
#     # New args for features
#     ap.add_argument("--sensitivity", type=float, default=0.5, help="Mouse speed scaling (0.1-2.0, default 0.5 for slower movement)")
#     ap.add_argument("--rate-limit-ms", type=int, default=50, help="Min ms between MQTT sends (10-200, default 50 for 20Hz)")
#     ap.add_argument("--inactivity-timeout-s", type=int, default=2, help="Seconds of key inactivity before release_all (default 2)")
#     ap.add_argument("--global-timeout-s", type=int, default=5, help="Seconds of total inactivity before flush (default 5)")
#     args = ap.parse_args()
#
#     print("🦆 HID-MQTT Forwarder starting...")
#
#     # Initialize MQTT forwarder with new params
#     mqtt_forwarder = MQTTHIDForwarder(args.broker, device_id=args.device_id,
#                                       sensitivity=args.sensitivity,
#                                       rate_limit_ms=args.rate_limit_ms,
#                                       inactivity_timeout_s=args.inactivity_timeout_s,
#                                       global_timeout_s=args.global_timeout_s)
#     # New: Set up signal handlers
#     signal.signal(signal.SIGINT, mqtt_forwarder.handle_sigint)  # CTRL+C
#     signal.signal(signal.SIGTSTP, mqtt_forwarder.handle_sigtstp)  # CTRL+Z (Linux/Unix; Windows may need alternative)
#
#     # Start input capture (reuse existing backends)
#     ok = (
#         start_evdev("", args.debug)  # Empty base URL since we're using MQTT
#         or start_pynput("", args.debug)
#         or start_pyautogui("", args.debug)
#     )
#
#     if not ok:
#         print("!! No usable input backend found – install 'python-evdev' or 'pynput' or 'pyautogui'.")
#         sys.exit(1)
#
#     try:
#         while True:
#             time.sleep(1)
#     except KeyboardInterrupt:
#         print("bye!")
#         mqtt_forwarder.client.loop_stop()
#         mqtt_forwarder.client.disconnect()
#
#
#
#
#
# #####
# #
# # #!/usr/bin/env python3
# # """
# # HID-remote v2  –  relay local mouse & keyboard activity
# # to an UltraWiFiDuck (ESP32-S2) via its “/mouse” and “/key” REST endpoints.
# #
# #  • Works on   Linux → evdev   |   X11/Wayland → pynput   |   any GUI → pyautogui (mouse-only)
# #  • All exceptions are trapped – the script never crashes
# #  • --debug    dumps every outgoing request
# #
# # (c) 2024  –  free to use under MIT Licence
# # """
# # from __future__ import annotations
# # import argparse, os, queue, sys, threading, time, urllib.request, urllib.error
# #
# # # ————
# # # Helpers
# # # ————
# # def api_get(base: str, path: str, dbg: bool, timeout: float = 1.5) -> bytes:
# #     """Tiny wrapper around urllib that never raises upstream."""
# #     url = f"{base}{path}"
# #     try:
# #         with urllib.request.urlopen(url, timeout=timeout) as r:
# #             data = r.read()
# #     except Exception as e:
# #         if dbg:
# #             print(f"[API] {e}  ←  {url}")
# #         return b""
# #     if dbg:
# #         print("→", url, data)
# #     return data
# #
# #
# # # ————
# # # Key-code lookup tables
# # # ————
# # EV2HID: dict[int, int] = {    # Linux evdev → Arduino HID
# #     1: 0xB1,  # Esc
# #     # number row
# #     2: ord('1'),  3: ord('2'),  4: ord('3'),  5: ord('4'),  6: ord('5'),
# #     7: ord('6'),  8: ord('7'),  9: ord('8'), 10: ord('9'), 11: ord('0'),
# #     12: ord('-'), 13: ord('='),
# #     # modifiers & editing
# #     14: 0xB2,  15: 0xB3, 28: 0xB0,
# #     29: 0x80,  42: 0x81, 54: 0x85,
# #     56: 0x82,  57: ord(' '),
# #     # alpha
# #     16: ord('q'), 17: ord('w'), 18: ord('e'), 19: ord('r'), 20: ord('t'),
# #     21: ord('y'), 22: ord('u'), 23: ord('i'), 24: ord('o'), 25: ord('p'),
# #     26: ord('['), 27: ord(']'),
# #     30: ord('a'), 31: ord('s'), 32: ord('d'), 33: ord('f'), 34: ord('g'),
# #     35: ord('h'), 36: ord('j'), 37: ord('k'), 38: ord('l'),
# #     39: ord(';'), 40: ord("'"), 41: ord('`'), 43: ord('\\'),
# #     44: ord('z'), 45: ord('x'), 46: ord('c'), 47: ord('v'), 48: ord('b'),
# #     49: ord('n'), 50: ord('m'),
# #     51: ord(','), 52: ord('.'), 53: ord('/'),
# #     # arrows
# #    105: 0xD8, 106: 0xD7, 103: 0xDA, 108: 0xD9,
# #    111: 0xD4,
# #     # F-keys
# #     59: 0xC2, 60: 0xC3, 61: 0xC4, 62: 0xC5, 63: 0xC6,
# #     64: 0xC7, 65: 0xC8, 66: 0xC9, 67: 0xCA, 68: 0xCB,
# #     87: 0xCC, 88: 0xCD,
# # }
# #
# # def vk2hid(vk: int) -> int | None:    # Windows VK / XKB → HID
# #     if 0x30 <= vk <= 0x39:    # 0-9
# #         return vk
# #     if 0x41 <= vk <= 0x5A:    # A-Z → a-z
# #         return vk + 32
# #     if vk in (0x25, 0x27, 0x26, 0x28):    # arrows
# #         return {0x25:0xD8, 0x27:0xD7, 0x26:0xDA, 0x28:0xD9}[vk]
# #     if 0x70 <= vk <= 0x7B:    # F1-F12
# #         return 0xC2 + (vk - 0x70)
# #     if vk == 0x2E:    # Delete
# #         return 0xD4
# #     return EV2HID.get(vk)
# #
# #
# # # ————
# # # Backend #1 – evdev  (Linux)
# # # ————
# # def start_evdev(base: str, dbg: bool) -> bool:
# #     try:
# #         from evdev import InputDevice, categorize, ecodes, list_devices  # type: ignore
# #     except ImportError:
# #         return False
# #
# #     devs: list[InputDevice] = []
# #     for path in list_devices():
# #         try:
# #             d = InputDevice(path)
# #             caps = d.capabilities()
# #             if ecodes.EV_REL in caps or ecodes.EV_ABS in caps:
# #                 devs.append(d)
# #         except Exception:
# #             pass
# #     if not devs:
# #         return False
# #     print(f"✔ evdev backend – {len(devs)} device(s)")
# #
# #     q: "queue.SimpleQueue" = queue.SimpleQueue()
# #
# #     def reader(dev: InputDevice):
# #         for ev in dev.read_loop():
# #             q.put(ev)
# #     for d in devs:
# #         threading.Thread(target=reader, args=(d,), daemon=True).start()
# #
# #     def mixer():
# #         dx = dy = wheel = 0
# #         last_flush = time.time()
# #         last_abs_x = last_abs_y = None  # Added back from v3 for ABS handling
# #         while True:
# #             try:
# #                 ev = q.get(timeout=0.03)
# #             except queue.Empty:
# #                 pass
# #             else:
# #                 if ev.type == ecodes.EV_REL:
# #                     if ev.code == ecodes.REL_X:    dx += ev.value
# #                     elif ev.code == ecodes.REL_Y:    dy += ev.value
# #                     elif ev.code == ecodes.REL_WHEEL: wheel += ev.value
# #                 elif ev.type == ecodes.EV_ABS:  # Added back from v3
# #                     if ev.code == ecodes.ABS_X:
# #                         if last_abs_x is not None:
# #                             dx += ev.value - last_abs_x
# #                         last_abs_x = ev.value
# #                     elif ev.code == ecodes.ABS_Y:
# #                         if last_abs_y is not None:
# #                             dy += ev.value - last_abs_y
# #                         last_abs_y = ev.value
# #                 elif ev.type == ecodes.EV_KEY:
# #                     hid = EV2HID.get(ev.code)
# #                     if hid:
# #                         api_get(base, f"/key?{'press' if ev.value else 'release'}={hid}", dbg)
# #             if (dx or dy or wheel) and time.time() - last_flush > 0.04:
# #                 # ── send in USB-legal chunks (-127 … +127) ────
# #                 while dx or dy or wheel:
# #                     step_x = max(-127, min(127, dx))
# #                     step_y = max(-127, min(127, dy))
# #                     step_w = max(-127, min(127, wheel))
# #                     api_get(base,
# #                             f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
# #                             dbg)
# #                     dx    -= step_x
# #                     dy    -= step_y
# #                     wheel -= step_w
# #                 last_flush = time.time()
# #     threading.Thread(target=mixer, daemon=True).start()
# #     return True
# #
# #
# # # ————
# # # Backend #2 – pynput  (X11 / Wayland / Windows)
# # # ————
# # def start_pynput(base: str, dbg: bool) -> bool:
# #     try:
# #         from pynput import mouse, keyboard    # type: ignore
# #     except Exception:
# #         return False
# #
# #     dx = dy = wheel = 0
# #     last_flush = time.time()
# #
# #     def flush():
# #         nonlocal dx, dy, wheel, last_flush
# #         while dx or dy or wheel:
# #             step_x = max(-127, min(127, int(round(dx))))
# #             step_y = max(-127, min(127, int(round(dy))))
# #             step_w = max(-127, min(127, int(round(wheel))))
# #             api_get(base,
# #                     f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
# #                     dbg)
# #             dx    -= step_x
# #             dy    -= step_y
# #             wheel -= step_w
# #         last_flush = time.time()
# #
# #     # mouse callbacks ----
# #     last_xy = [None, None]
# #
# #     def on_move(x, y):
# #         nonlocal dx, dy
# #         if last_xy[0] is not None:
# #             dx += (x - last_xy[0]) * 0.1  # Restored scaling from v3 for smoothness
# #             dy += (y - last_xy[1]) * 0.1
# #         last_xy[:] = [x, y]
# #         if time.time() - last_flush > 0.04:
# #             flush()
# #
# #     def on_scroll(_x, _y, _dx, _dy):
# #         nonlocal wheel
# #         wheel += _dy
# #         flush()
# #
# #     mouse.Listener(on_move=on_move, on_scroll=on_scroll).start()
# #
# #     # keyboard callbacks ----
# #     def on_press(k):
# #         vk = getattr(k, "vk", getattr(k, "value", k).vk)
# #         hid = vk2hid(vk)
# #         if hid:
# #             api_get(base, f"/key?press={hid}", dbg)
# #
# #     def on_release(k):
# #         vk = getattr(k, "vk", getattr(k, "value", k).vk)
# #         hid = vk2hid(vk)
# #         if hid:
# #             api_get(base, f"/key?release={hid}", dbg)
# #
# #     keyboard.Listener(on_press=on_press, on_release=on_release).start()
# #     print("✔ pynput backend")
# #     return True
# #
# #
# # # ————
# # # Backend #3 – pyautogui  (mouse only, no keys – fallback)
# # # ————
# # def start_pyautogui(base: str, dbg: bool) -> bool:
# #     try:
# #         import pyautogui    # type: ignore
# #     except Exception:
# #         return False
# #
# #     dx = dy = wheel = 0
# #     last_flush = time.time()
# #     last_pos = pyautogui.position()
# #
# #     def loop():
# #         nonlocal dx, dy, wheel, last_flush, last_pos
# #         while True:
# #             pos = pyautogui.position()
# #             dx += pos.x - last_pos.x
# #             dy += pos.y - last_pos.y
# #             last_pos = pos
# #             # Stub for wheel (pyautogui doesn't directly support, but you could add polling if needed)
# #             # For now, no wheel in pyautogui
# #             if (dx or dy) and time.time() - last_flush > 0.04:
# #                 while dx or dy or wheel:
# #                     step_x = max(-127, min(127, dx))
# #                     step_y = max(-127, min(127, dy))
# #                     step_w = max(-127, min(127, wheel))
# #                     api_get(base, f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}", dbg)
# #                     dx -= step_x
# #                     dy -= step_y
# #                     wheel -= step_w
# #                 last_flush = time.time()
# #             time.sleep(0.01)
# #     threading.Thread(target=loop, daemon=True).start()
# #     print("✔ pyautogui fallback (mouse only)")
# #     return True
# #
# #
# # # ————
# # # main
# # # ————
# # if __name__ == "__main__":
# #     ap = argparse.ArgumentParser(description="Relay local HID input to UltraWiFiDuck")
# #     ap.add_argument("--url", default="http://192.168.1.35:81",
# #                     help="base URL of the duck (no trailing slash)")
# #     ap.add_argument("--debug", action="store_true", help="print every REST call")
# #     args = ap.parse_args()
# #
# #     print("🦆  HID-remote v2 – starting …")
# #
# #     ok = (
# #         start_evdev(args.url, args.debug)
# #         or start_pynput(args.url, args.debug)
# #         or start_pyautogui(args.url, args.debug)
# #     )
# #     if not ok:
# #         print("!!  No usable input backend found – install 'python-evdev' or 'pynput' or 'pyautogui'.")
# #         sys.exit(1)
# #
# #     try:
# #         while True:
# #             time.sleep(1)
# #     except KeyboardInterrupt:
# #         print("bye!")
#
#
# #####
#
#
# # #!/usr/bin/env python3
# # """
# # MQTT HID Forwarder - Forwards HID events over MQTT
# # Based on HID_remote.py but sends data via MQTT instead of direct HTTP
# # """
# # from __future__ import annotations
# # import argparse, os, queue, sys, threading, time, urllib.request, urllib.error
# #
# # import json
# # import time
# # import threading
# # import queue
# # import paho.mqtt.client as mqtt
# # #from HID_remote import start_evdev, start_pynput, start_pyautogui
# #
# # class MQTTHIDForwarder:
# #     def __init__(self, mqtt_broker="broker.emqx.io", mqtt_port=1883, device_id="esp32_hid_001"):
# #         self.mqtt_broker = mqtt_broker
# #         self.mqtt_port = mqtt_port
# #         self.device_id = device_id
# #         self.client = mqtt.Client()
# #         self.command_queue = queue.Queue()
# #
# #         # MQTT topics
# #         self.mouse_topic = f"hid/{device_id}/mouse"
# #         self.key_topic = f"hid/{device_id}/key"
# #         self.status_topic = f"hid/{device_id}/status"
# #
# #         self.setup_mqtt()
# #
# #     def setup_mqtt(self):
# #         self.client.on_connect = self.on_connect
# #         self.client.on_disconnect = self.on_disconnect
# #         self.client.connect(self.mqtt_broker, self.mqtt_port, 60)
# #         self.client.loop_start()
# #
# #     def on_connect(self, client, userdata, flags, rc):
# #         print(f"✔ Connected to MQTT broker with result code {rc}")
# #         # Publish online status
# #         client.publish(self.status_topic, json.dumps({"status": "online", "timestamp": time.time()}))
# #
# #     def on_disconnect(self, client, userdata, rc):
# #         print(f"✗ Disconnected from MQTT broker with result code {rc}")
# #
# #     def send_mouse_command(self, dx=0, dy=0, wheel=0):
# #         """Send mouse movement command via MQTT"""
# #         command = {
# #             "dx": dx,
# #             "dy": dy,
# #             "wheel": wheel,
# #             "timestamp": time.time()
# #         }
# #         self.client.publish(self.mouse_topic, json.dumps(command))
# #
# #     def send_key_command(self, action, key_code):
# #         """Send key press/release command via MQTT"""
# #         command = {
# #             "action": action,  # "press" or "release"
# #             "key": key_code,
# #             "timestamp": time.time()
# #         }
# #         self.client.publish(self.key_topic, json.dumps(command))
# #
# # # Modified API functions to use MQTT
# # mqtt_forwarder = None
# #
# # def api_get(base: str, path: str, dbg: bool, timeout: float = 1.5) -> bytes:
# #     """Modified to send via MQTT instead of HTTP"""
# #     global mqtt_forwarder
# #
# #     if not mqtt_forwarder:
# #         return b""
# #
# #     try:
# #         if "/mouse?" in path:
# #             # Parse mouse parameters
# #             params = {}
# #             if "dx=" in path:
# #                 params["dx"] = int(path.split("dx=")[1].split("&")[0])
# #             if "dy=" in path:
# #                 params["dy"] = int(path.split("dy=")[1].split("&")[0])
# #             if "wheel=" in path:
# #                 params["wheel"] = int(path.split("wheel=")[1].split("&")[0])
# #
# #             mqtt_forwarder.send_mouse_command(
# #                 params.get("dx", 0),
# #                 params.get("dy", 0),
# #                 params.get("wheel", 0)
# #             )
# #
# #         elif "/key?" in path:
# #             # Parse key parameters
# #             if "press=" in path:
# #                 key_code = int(path.split("press=")[1].split("&")[0])
# #                 mqtt_forwarder.send_key_command("press", key_code)
# #             elif "release=" in path:
# #                 key_code = int(path.split("release=")[1].split("&")[0])
# #                 mqtt_forwarder.send_key_command("release", key_code)
# #             else:
# #                 mqtt_forwarder.send_key_command("release_all", 0)
# #
# #         if dbg:
# #             print(f"→ MQTT: {path}")
# #
# #     except Exception as e:
# #         if dbg:
# #             print(f"[MQTT] {e} ← {path}")
# #
# #     return b"OK"
# #
# # if __name__ == "__main__":
# #     import argparse
# #
# #     ap = argparse.ArgumentParser(description="Forward HID input via MQTT")
# #     ap.add_argument("--broker", default="broker.emqx.io", help="MQTT broker address")
# #     ap.add_argument("--device-id", default="esp32_hid_001", help="Unique device ID")
# #     ap.add_argument("--debug", action="store_true", help="print every MQTT message")
# #     args = ap.parse_args()
# #
# #     print("🦆 HID-MQTT Forwarder starting...")
# #
# #     # Initialize MQTT forwarder
# #     mqtt_forwarder = MQTTHIDForwarder(args.broker, device_id=args.device_id)
# #
# #     # Start input capture (reuse existing backends)
# #     ok = (
# #         start_evdev("", args.debug)  # Empty base URL since we're using MQTT
# #         or start_pynput("", args.debug)
# #         or start_pyautogui("", args.debug)
# #     )
# #
# #     if not ok:
# #         print("!! No usable input backend found")
# #         exit(1)
# #
# #     try:
# #         while True:
# #             time.sleep(1)
# #     except KeyboardInterrupt:
# #         print("bye!")
# #         mqtt_forwarder.client.loop_stop()
# #         mqtt_forwarder.client.disconnect()
# #
# #
# # # #!/usr/bin/env python3
# # """
# # HID-remote v2  –  relay local mouse & keyboard activity
# # to an UltraWiFiDuck (ESP32-S2) via its “/mouse” and “/key” REST endpoints.
# #
# #  • Works on   Linux → evdev   |   X11/Wayland → pynput   |   any GUI → pyautogui (mouse-only)
# #  • All exceptions are trapped – the script never crashes
# #  • --debug    dumps every outgoing request
# #
# # (c) 2024  –  free to use under MIT Licence
# # """
# # # from __future__ import annotations
# # # import argparse, os, queue, sys, threading, time, urllib.request, urllib.error
# #
# # # ————
# # # Helpers
# # # ————
# # def api_get(base: str, path: str, dbg: bool, timeout: float = 1.5) -> bytes:
# #     """Tiny wrapper around urllib that never raises upstream."""
# #     url = f"{base}{path}"
# #     try:
# #         with urllib.request.urlopen(url, timeout=timeout) as r:
# #             data = r.read()
# #     except Exception as e:
# #         if dbg:
# #             print(f"[API] {e}  ←  {url}")
# #         return b""
# #     if dbg:
# #         print("→", url, data)
# #     return data
# #
# #
# # # ————
# # # Key-code lookup tables
# # # ————
# # EV2HID: dict[int, int] = {    # Linux evdev → Arduino HID
# #     1: 0xB1,  # Esc
# #     # number row
# #     2: ord('1'),  3: ord('2'),  4: ord('3'),  5: ord('4'),  6: ord('5'),
# #     7: ord('6'),  8: ord('7'),  9: ord('8'), 10: ord('9'), 11: ord('0'),
# #     12: ord('-'), 13: ord('='),
# #     # modifiers & editing
# #     14: 0xB2,  15: 0xB3, 28: 0xB0,
# #     29: 0x80,  42: 0x81, 54: 0x85,
# #     56: 0x82,  57: ord(' '),
# #     # alpha
# #     16: ord('q'), 17: ord('w'), 18: ord('e'), 19: ord('r'), 20: ord('t'),
# #     21: ord('y'), 22: ord('u'), 23: ord('i'), 24: ord('o'), 25: ord('p'),
# #     26: ord('['), 27: ord(']'),
# #     30: ord('a'), 31: ord('s'), 32: ord('d'), 33: ord('f'), 34: ord('g'),
# #     35: ord('h'), 36: ord('j'), 37: ord('k'), 38: ord('l'),
# #     39: ord(';'), 40: ord("'"), 41: ord('`'), 43: ord('\\'),
# #     44: ord('z'), 45: ord('x'), 46: ord('c'), 47: ord('v'), 48: ord('b'),
# #     49: ord('n'), 50: ord('m'),
# #     51: ord(','), 52: ord('.'), 53: ord('/'),
# #     # arrows
# #    105: 0xD8, 106: 0xD7, 103: 0xDA, 108: 0xD9,
# #    111: 0xD4,
# #     # F-keys
# #     59: 0xC2, 60: 0xC3, 61: 0xC4, 62: 0xC5, 63: 0xC6,
# #     64: 0xC7, 65: 0xC8, 66: 0xC9, 67: 0xCA, 68: 0xCB,
# #     87: 0xCC, 88: 0xCD,
# # }
# #
# # def vk2hid(vk: int) -> int | None:    # Windows VK / XKB → HID
# #     if 0x30 <= vk <= 0x39:    # 0-9
# #         return vk
# #     if 0x41 <= vk <= 0x5A:    # A-Z → a-z
# #         return vk + 32
# #     if vk in (0x25, 0x27, 0x26, 0x28):    # arrows
# #         return {0x25:0xD8, 0x27:0xD7, 0x26:0xDA, 0x28:0xD9}[vk]
# #     if 0x70 <= vk <= 0x7B:    # F1-F12
# #         return 0xC2 + (vk - 0x70)
# #     if vk == 0x2E:    # Delete
# #         return 0xD4
# #     return EV2HID.get(vk)
# #
# #
# # # ————
# # # Backend #1 – evdev  (Linux)
# # # ————
# # def start_evdev(base: str, dbg: bool) -> bool:
# #     try:
# #         from evdev import InputDevice, categorize, ecodes, list_devices  # type: ignore
# #     except ImportError:
# #         return False
# #
# #     devs: list[InputDevice] = []
# #     for path in list_devices():
# #         try:
# #             d = InputDevice(path)
# #             caps = d.capabilities()
# #             if ecodes.EV_REL in caps or ecodes.EV_ABS in caps:
# #                 devs.append(d)
# #         except Exception:
# #             pass
# #     if not devs:
# #         return False
# #     print(f"✔ evdev backend – {len(devs)} device(s)")
# #
# #     q: "queue.SimpleQueue" = queue.SimpleQueue()
# #
# #     def reader(dev: InputDevice):
# #         for ev in dev.read_loop():
# #             q.put(ev)
# #     for d in devs:
# #         threading.Thread(target=reader, args=(d,), daemon=True).start()
# #
# #     def mixer():
# #         dx = dy = wheel = 0
# #         last_flush = time.time()
# #         last_abs_x = last_abs_y = None  # Added back from v3 for ABS handling
# #         while True:
# #             try:
# #                 ev = q.get(timeout=0.03)
# #             except queue.Empty:
# #                 pass
# #             else:
# #                 if ev.type == ecodes.EV_REL:
# #                     if ev.code == ecodes.REL_X:    dx += ev.value
# #                     elif ev.code == ecodes.REL_Y:    dy += ev.value
# #                     elif ev.code == ecodes.REL_WHEEL: wheel += ev.value
# #                 elif ev.type == ecodes.EV_ABS:  # Added back from v3
# #                     if ev.code == ecodes.ABS_X:
# #                         if last_abs_x is not None:
# #                             dx += ev.value - last_abs_x
# #                         last_abs_x = ev.value
# #                     elif ev.code == ecodes.ABS_Y:
# #                         if last_abs_y is not None:
# #                             dy += ev.value - last_abs_y
# #                         last_abs_y = ev.value
# #                 elif ev.type == ecodes.EV_KEY:
# #                     hid = EV2HID.get(ev.code)
# #                     if hid:
# #                         api_get(base, f"/key?{'press' if ev.value else 'release'}={hid}", dbg)
# #             if (dx or dy or wheel) and time.time() - last_flush > 0.04:
# #                 # ── send in USB-legal chunks (-127 … +127) ────
# #                 while dx or dy or wheel:
# #                     step_x = max(-127, min(127, dx))
# #                     step_y = max(-127, min(127, dy))
# #                     step_w = max(-127, min(127, wheel))
# #                     api_get(base,
# #                             f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
# #                             dbg)
# #                     dx    -= step_x
# #                     dy    -= step_y
# #                     wheel -= step_w
# #                 last_flush = time.time()
# #     threading.Thread(target=mixer, daemon=True).start()
# #     return True
# #
# #
# # # ————
# # # Backend #2 – pynput  (X11 / Wayland / Windows)
# # # ————
# # def start_pynput(base: str, dbg: bool) -> bool:
# #     try:
# #         from pynput import mouse, keyboard    # type: ignore
# #     except Exception:
# #         return False
# #
# #     dx = dy = wheel = 0
# #     last_flush = time.time()
# #
# #     def flush():
# #         nonlocal dx, dy, wheel, last_flush
# #         while dx or dy or wheel:
# #             step_x = max(-127, min(127, int(round(dx))))
# #             step_y = max(-127, min(127, int(round(dy))))
# #             step_w = max(-127, min(127, int(round(wheel))))
# #             api_get(base,
# #                     f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}",
# #                     dbg)
# #             dx    -= step_x
# #             dy    -= step_y
# #             wheel -= step_w
# #         last_flush = time.time()
# #
# #     # mouse callbacks ----
# #     last_xy = [None, None]
# #
# #     def on_move(x, y):
# #         nonlocal dx, dy
# #         if last_xy[0] is not None:
# #             dx += (x - last_xy[0]) * 0.1  # Restored scaling from v3 for smoothness
# #             dy += (y - last_xy[1]) * 0.1
# #         last_xy[:] = [x, y]
# #         if time.time() - last_flush > 0.04:
# #             flush()
# #
# #     def on_scroll(_x, _y, _dx, _dy):
# #         nonlocal wheel
# #         wheel += _dy
# #         flush()
# #
# #     mouse.Listener(on_move=on_move, on_scroll=on_scroll).start()
# #
# #     # keyboard callbacks ----
# #     def on_press(k):
# #         vk = getattr(k, "vk", getattr(k, "value", k).vk)
# #         hid = vk2hid(vk)
# #         if hid:
# #             api_get(base, f"/key?press={hid}", dbg)
# #
# #     def on_release(k):
# #         vk = getattr(k, "vk", getattr(k, "value", k).vk)
# #         hid = vk2hid(vk)
# #         if hid:
# #             api_get(base, f"/key?release={hid}", dbg)
# #
# #     keyboard.Listener(on_press=on_press, on_release=on_release).start()
# #     print("✔ pynput backend")
# #     return True
# #
# #
# # # ————
# # # Backend #3 – pyautogui  (mouse only, no keys – fallback)
# # # ————
# # def start_pyautogui(base: str, dbg: bool) -> bool:
# #     try:
# #         import pyautogui    # type: ignore
# #     except Exception:
# #         return False
# #
# #     dx = dy = wheel = 0
# #     last_flush = time.time()
# #     last_pos = pyautogui.position()
# #
# #     def loop():
# #         nonlocal dx, dy, wheel, last_flush, last_pos
# #         while True:
# #             pos = pyautogui.position()
# #             dx += pos.x - last_pos.x
# #             dy += pos.y - last_pos.y
# #             last_pos = pos
# #             # Stub for wheel (pyautogui doesn't directly support, but you could add polling if needed)
# #             # For now, no wheel in pyautogui
# #             if (dx or dy) and time.time() - last_flush > 0.04:
# #                 while dx or dy or wheel:
# #                     step_x = max(-127, min(127, dx))
# #                     step_y = max(-127, min(127, dy))
# #                     step_w = max(-127, min(127, wheel))
# #                     api_get(base, f"/mouse?dx={step_x}&dy={step_y}&wheel={step_w}", dbg)
# #                     dx -= step_x
# #                     dy -= step_y
# #                     wheel -= step_w
# #                 last_flush = time.time()
# #             time.sleep(0.01)
# #     threading.Thread(target=loop, daemon=True).start()
# #     print("✔ pyautogui fallback (mouse only)")
# #     return True
# #
# #
# # # ————
# # # main
# # # ————
# # # if __name__ == "__main__":
# # #     ap = argparse.ArgumentParser(description="Relay local HID input to UltraWiFiDuck")
# # #     ap.add_argument("--url", default="http://192.168.1.35:81",
# # #                     help="base URL of the duck (no trailing slash)")
# # #     ap.add_argument("--debug", action="store_true", help="print every REST call")
# # #     args = ap.parse_args()
# # #
# # #     print("🦆  HID-remote v2 – starting …")
# # #
# # #     ok = (
# # #         start_evdev(args.url, args.debug)
# # #         or start_pynput(args.url, args.debug)
# # #         or start_pyautogui(args.url, args.debug)
# # #     )
# # #     if not ok:
# # #         print("!!  No usable input backend found – install 'python-evdev' or 'pynput' or 'pyautogui'.")
# # #         sys.exit(1)
# # #
# # #     try:
# # #         while True:
# # #             time.sleep(1)
# # #     except KeyboardInterrupt:
# # #         print("bye!")
