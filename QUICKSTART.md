# Quick Start Guide - MQTT HID Tunnel

## Overview

This system tunnels local keyboard and mouse input over MQTT to an ESP32-S2 "UltraWiFiDuck" that presents itself as a USB HID device to a target machine. Perfect for remote administration, penetration testing, or IoT control.

## Prerequisites

### Hardware
- ESP32-S2 development board (e.g., UltraWiFiDuck)
- USB cable for programming ESP32
- Target machine with USB port

### Software
- **Python 3.7+** with pip
- **PlatformIO** (for ESP32 firmware compilation)
- Linux, macOS, or Windows

## Step 1: Install Python Dependencies

```bash
# Install required packages
pip install paho-mqtt

# Optional: For better input capture
pip install python-evdev   # Linux only
pip install pynput         # Cross-platform
pip install pyautogui      # Fallback
```

## Step 2: Flash ESP32 Firmware

1. Navigate to the firmware directory:
   ```bash
   cd /home/user/HID_Tunnel/UltraWiFiDuck
   ```

2. Configure WiFi credentials (if needed):
   - Edit `src/config.h` or `platformio.ini`
   - Set your WiFi SSID and password

3. Build and flash:
   ```bash
   pio run -t upload
   ```

4. Monitor serial output (optional):
   ```bash
   pio device monitor
   ```

## Step 3: Run Python Host

### Basic Usage (Single Broker)
```bash
cd /home/user/HID_Tunnel
python HID_remote.py --device-id esp32_hid_001
```

### Multi-Broker for Reliability
```bash
python HID_remote.py \
  --brokers broker.emqx.io test.mosquitto.org \
  --device-id esp32_hid_001
```

### State-Based Keyboard (Recommended for Slow Links)
```bash
python HID_remote.py \
  --keyboard-state \
  --brokers broker.emqx.io test.mosquitto.org \
  --device-id esp32_hid_001
```

### High-Performance Mode (Lower Latency)
```bash
python HID_remote.py \
  --rate-limit-ms 15 \
  --sensitivity 1.0 \
  --brokers broker.emqx.io test.mosquitto.org \
  --device-id esp32_hid_001
```

## Step 4: Verify Connection

You should see output like:
```
🦆 HID-MQTT Forwarder starting...
Multi-broker mode: 2 broker(s)
Connecting to broker.emqx.io:1883... (attempt 1)
✓ Connection initiated to broker.emqx.io:1883
✔ Connected to broker.emqx.io:1883 (rc=0)
Sent discovery ping on broker.emqx.io:1883
✓✓ Device discovered on broker.emqx.io:1883 - now active HID broker
✔ evdev backend – 3 device(s)
[Active broker: broker.emqx.io:1883]
```

On the ESP32 serial monitor:
```
[MQTT] ✓ Connected
Subscribed to: mouse (QoS 0), key (QoS 1), ping (QoS 1)
[PING] Responded to host discovery
```

## Step 5: Test Input

1. Move your mouse → Target machine cursor should move
2. Click mouse buttons → Target machine should register clicks
3. Type on keyboard → Target machine should receive keystrokes
4. Try key combos (Ctrl+C, Ctrl+Alt+Del, etc.)

## Common Issues & Solutions

### "No active broker" / Device not found
- **Check device ID matches**: Python `--device-id` must match ESP32 `DEVICE_ID`
- **Verify ESP32 WiFi**: Check serial monitor for WiFi connection status
- **Test broker connectivity**: `ping broker.emqx.io`
- **Wait 10 seconds**: Discovery can take a few seconds

### Input lag / sluggish response
- **Reduce rate limit**: `--rate-limit-ms 15` (default is 20ms)
- **Increase sensitivity**: `--sensitivity 1.5` (default is 0.5)
- **Check network ping**: High latency to broker affects performance
- **Use fewer brokers**: Multi-broker adds discovery overhead

### Stuck keys after disconnect
- **Enable state-based keyboard**: `--keyboard-state`
- **Check timeout**: Default is 2 seconds, adjust with `--inactivity-timeout-s`
- **Verify HID timeout**: ESP32 releases keys after 1 second inactivity

### Mouse not moving
- **Check input backend**: Look for "✔ evdev backend" or "✔ pynput backend"
- **Install input libraries**: `pip install python-evdev pynput`
- **Run as root** (Linux): `sudo python HID_remote.py` (for evdev access)

### Keyboard not working
- **Verify key mapping**: Some keys may not be mapped (check EV2HID table)
- **Check HID interval**: Try increasing `MIN_HID_INTERVAL_MS` in firmware
- **Test with simple keys**: Try 'a', 'b', 'c' before complex combos

## Advanced Configuration

### Custom Broker Configuration (ESP32)

Edit `UltraWiFiDuck/src/duck_control_web.cpp`:

```cpp
BrokerConfig MQTT_BROKERS[] = {
    {"your-broker.com", 1883},
    {"backup-broker.com", 1883},
};
```

Then rebuild and reflash:
```bash
pio run -t upload
```

### Performance Tuning

**For low-latency local networks:**
```bash
python HID_remote.py \
  --rate-limit-ms 10 \
  --sensitivity 1.5 \
  --inactivity-timeout-s 5
```

**For slow/unreliable internet:**
```bash
python HID_remote.py \
  --keyboard-state \
  --rate-limit-ms 50 \
  --sensitivity 0.3 \
  --brokers broker1 broker2 broker3
```

### Signal Handling (Ctrl+C / Ctrl+Z)

The Python host intercepts Ctrl+C and Ctrl+Z and relays them to the target:
- **First 3 times**: Sends Ctrl+C or Ctrl+Z to target
- **4th time**: Exits the script

To force exit immediately, press Ctrl+C four times rapidly.

## Architecture Overview

```
┌─────────────────┐        ┌──────────────┐        ┌─────────────────┐
│  Operator PC    │        │ MQTT Broker  │        │  Target Machine │
│                 │        │ (Internet)   │        │                 │
│  HID_remote.py  │◄──────►│              │◄──────►│   ESP32-S2      │
│  (Python host)  │        │ broker.emqx  │        │   (HID device)  │
│                 │        │              │        │                 │
│  Mouse/Keyboard │        │              │        │   USB HID       │
│  Input Capture  │        │              │        │   Output        │
└─────────────────┘        └──────────────┘        └─────────────────┘
         │                         │                         │
         │  Mouse/Key events       │  MQTT messages          │  HID reports
         │  QoS 0 (mouse)          │  QoS 0/1                │  USB packets
         │  QoS 1 (keyboard)       │                         │
         └────────────────────────────────────────────────────┘
```

## Next Steps

- Read `MQTT_HID_IMPROVEMENTS.md` for detailed technical documentation
- Experiment with different broker configurations
- Try state-based keyboard for better reliability
- Adjust latency/sensitivity for your use case
- Monitor ESP32 serial output for diagnostics

## Getting Help

1. Check serial monitor output on ESP32
2. Enable debug mode: `--debug`
3. Review logs for connection/discovery issues
4. Verify all device IDs match
5. Test with single broker before multi-broker

## Security Considerations

⚠️ **Warning**: This system sends unencrypted keyboard/mouse data over public MQTT brokers.

- **Do NOT use** for sensitive operations (passwords, credentials)
- **Consider using** private MQTT brokers with authentication
- **Enable TLS/SSL** if supported by your brokers
- **Use VPN** for additional encryption layer
- **Restrict broker access** with firewall rules

## Credits

Based on the UltraWiFiDuck project. Enhanced with multi-broker support, state-based protocols, and performance optimizations.

MIT License - Free to use and modify.
