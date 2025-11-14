#!/usr/bin/env python3
"""
HID Tunnel v5.0 - Configuration Helper
Displays network configuration and generates ESP32 setup code
"""

import socket
import sys
import argparse

def get_local_ip():
    """Auto-detect local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception as e:
        print(f"⚠️  Could not auto-detect IP: {e}")
        return "127.0.0.1"

def show_network_info(device_id, ws_port, http_port):
    """Display network configuration."""
    local_ip = get_local_ip()

    print("╔════════════════════════════════════════════════════════════════╗")
    print("║        HID Tunnel v5.0 - Configuration Helper                 ║")
    print("╚════════════════════════════════════════════════════════════════╝")
    print()
    print("📡 NETWORK CONFIGURATION")
    print("=" * 64)
    print(f"  Local IP Address:  {local_ip}")
    print(f"  Device ID:         {device_id}")
    print()
    print("  Transport Endpoints:")
    print(f"    MQTT:      broker.emqx.io:1883, test.mosquitto.org:1883")
    print(f"    WebSocket: ws://{local_ip}:{ws_port}/")
    print(f"    HTTP:      http://{local_ip}:{http_port}/")
    print(f"    mDNS:      UDP broadcast on port 37020")
    print()

    # Generate ESP32 configuration
    print("🔧 ESP32 CONFIGURATION (duck_control_web_v5.cpp)")
    print("=" * 64)
    print()
    print("1. Update the DEVICE_ID constant:")
    print(f'   const char* DEVICE_ID = "{device_id}";')
    print()

    print("2. Configure static fallback endpoints (optional, mDNS will auto-discover):")
    print()
    print("   // WebSocket endpoints (if mDNS fails)")
    print("   WSEndpointConfig WS_ENDPOINTS[] = {")
    print(f'       {{"{local_ip}", {ws_port}, "/"}},')
    print("   };")
    print()
    print("   // HTTP endpoints (if mDNS fails)")
    print("   HTTPEndpointConfig HTTP_ENDPOINTS[] = {")
    print(f'       {{"{local_ip}", {http_port}}},')
    print("   };")
    print()

    print("3. MQTT brokers are already configured (no changes needed)")
    print("   - broker.emqx.io:1883")
    print("   - test.mosquitto.org:1883")
    print()

    # Startup instructions
    print("🚀 STARTUP SEQUENCE")
    print("=" * 64)
    print()
    print("OPTION A: Quick Start (Auto-Discovery)")
    print("-" * 64)
    print("  1. Flash ESP32 with v5 firmware")
    print("  2. Run: ./quick_start.sh")
    print("  3. ESP32 will auto-discover via mDNS - no manual config needed!")
    print()

    print("OPTION B: Manual Start")
    print("-" * 64)
    print("  1. Flash ESP32 with v5 firmware")
    print(f"  2. Run: python3 HID_remote_v5.py --transport auto \\")
    print(f"            --brokers broker.emqx.io test.mosquitto.org \\")
    print(f"            --ws-port {ws_port} \\")
    print(f"            --http-port {http_port} \\")
    print(f"            --device-id {device_id} \\")
    print(f"            --keyboard-state")
    print()

    # Troubleshooting
    print("🔍 TROUBLESHOOTING")
    print("=" * 64)
    print()
    print("  If ESP32 doesn't connect:")
    print("  1. Check serial monitor - ESP32 prints connection attempts")
    print("  2. Verify WiFi credentials in ESP32 firmware")
    print("  3. Ensure firewall allows ports 1883, 8765, 8080, 37020")
    print("  4. Try each transport individually:")
    print("     - MQTT only:      --transport mqtt")
    print("     - WebSocket only: --transport ws")
    print("     - HTTP only:      --transport http")
    print()
    print("  Check Python host is reachable:")
    print(f"    ping {local_ip}")
    print(f"    curl http://{local_ip}:{http_port}/poll?device_id={device_id}")
    print()

    # PlatformIO dependencies
    print("📦 PLATFORMIO DEPENDENCIES")
    print("=" * 64)
    print()
    print("  Add to platformio.ini:")
    print()
    print("  lib_deps = ")
    print("      me-no-dev/AsyncTCP @ ^1.1.1")
    print("      me-no-dev/ESPAsyncTCP @ ^1.2.2")
    print("      marvinroger/AsyncMqttClient @ ^0.9.0")
    print("      Links2004/WebSockets @ ^2.4.0")
    print("      bblanchon/ArduinoJson @ ^6.21.0")
    print()

    print("✅ Configuration complete! Ready to run.")
    print()

def main():
    parser = argparse.ArgumentParser(description="HID Tunnel v5.0 Configuration Helper")
    parser.add_argument("--device-id", default="esp32_hid_001", help="Device ID (must match ESP32)")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP port")

    args = parser.parse_args()

    show_network_info(args.device_id, args.ws_port, args.http_port)

if __name__ == "__main__":
    main()
