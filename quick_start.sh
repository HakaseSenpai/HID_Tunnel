#!/bin/bash
# HID Tunnel v5.0 - Quick Start Script
# Launches the Python host with smart defaults (auto-discovery mode)

set -e

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║        HID Tunnel v5.0 - Quick Start                          ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: python3 not found. Please install Python 3.7+."
    exit 1
fi

# Check if HID_remote_v5.py exists
if [ ! -f "HID_remote_v5.py" ]; then
    echo "❌ Error: HID_remote_v5.py not found in current directory."
    echo "   Please run this script from the HID_Tunnel directory."
    exit 1
fi

# Install dependencies if needed
echo "📦 Checking dependencies..."
python3 -c "import paho.mqtt" 2>/dev/null || {
    echo "   Installing paho-mqtt..."
    pip3 install paho-mqtt
}

python3 -c "import websockets" 2>/dev/null || {
    echo "   Installing websockets (optional, recommended)..."
    pip3 install websockets || echo "   ⚠️  websockets install failed - WebSocket transport unavailable"
}

echo ""
echo "🚀 Starting HID Tunnel in AUTO mode..."
echo "   - MQTT: broker.emqx.io, test.mosquitto.org"
echo "   - WebSocket: Port 8765"
echo "   - HTTP: Port 8080"
echo "   - Device ID: esp32_hid_001"
echo ""
echo "Press Ctrl+C to stop."
echo ""

# Launch with auto mode
python3 HID_remote_v5.py \
    --transport auto \
    --brokers broker.emqx.io test.mosquitto.org \
    --ws-port 8765 \
    --http-port 8080 \
    --device-id esp32_hid_001 \
    --keyboard-state
