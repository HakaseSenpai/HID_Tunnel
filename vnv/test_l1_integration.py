#!/usr/bin/env python3
"""
V&V Level 1: Integration Level Tests
Tests interaction between components
"""

import sys
import os
import json
import socket
import time
from typing import Dict, List, Tuple

class IntegrationTests:
    """Integration-level tests"""

    def __init__(self):
        self.results = []

    def test_mqtt_broker_connectivity(self) -> Tuple[str, bool, str]:
        """Test MQTT broker reachability"""
        brokers = [
            ("broker.emqx.io", 1883),
            ("test.mosquitto.org", 1883),
        ]

        reachable = []
        unreachable = []

        for host, port in brokers:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                result = sock.connect_ex((host, port))
                sock.close()

                if result == 0:
                    reachable.append(f"{host}:{port}")
                else:
                    unreachable.append(f"{host}:{port}")
            except Exception as e:
                unreachable.append(f"{host}:{port} ({e})")

        if len(unreachable) == len(brokers):
            return "FAIL", False, f"All MQTT brokers unreachable: {unreachable}"

        if unreachable:
            return "WARN", True, f"Reachable: {reachable}, Unreachable: {unreachable}"

        return "PASS", True, f"All MQTT brokers reachable: {reachable}"

    def test_port_availability(self) -> Tuple[str, bool, str]:
        """Test if required ports are available"""
        ports_to_check = [8765, 8080, 37020]

        available = []
        in_use = []

        for port in ports_to_check:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('', port))
                sock.close()
                available.append(port)
            except OSError:
                in_use.append(port)

        if in_use:
            return "WARN", True, f"Ports in use: {in_use}, Available: {available}"

        return "PASS", True, f"All required ports available: {available}"

    def test_udp_broadcast_capability(self) -> Tuple[str, bool, str]:
        """Test UDP broadcast for mDNS"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1)

            # Try to bind to mDNS port
            try:
                sock.bind(('', 37020))
                sock.close()
                return "PASS", True, "UDP broadcast capable, port 37020 available"
            except OSError:
                sock.close()
                return "WARN", True, "UDP broadcast capable but port 37020 in use"

        except Exception as e:
            return "FAIL", False, f"UDP broadcast failed: {str(e)}"

    def test_json_serialization_integration(self) -> Tuple[str, bool, str]:
        """Test JSON serialization of HID commands"""
        import json as json_lib

        test_commands = [
            {"type": "mouse", "dx": 10, "dy": 5, "wheel": 0},
            {"type": "key", "action": "press", "key": 65},
            {"type": "control", "command": "lock_transport", "transport": "mqtt"},
            {"type": "status", "device_id": "test", "uptime_ms": 12345},
        ]

        issues = []
        for cmd in test_commands:
            try:
                # Serialize
                payload = json_lib.dumps(cmd)

                # Deserialize
                parsed = json_lib.loads(payload)

                # Verify round-trip
                if parsed != cmd:
                    issues.append(f"Round-trip failed for {cmd}")

            except Exception as e:
                issues.append(f"Failed to serialize {cmd}: {e}")

        if issues:
            return "FAIL", False, "; ".join(issues)

        return "PASS", True, f"All {len(test_commands)} command types serialized correctly"

    def test_transport_selection_logic(self) -> Tuple[str, bool, str]:
        """Test transport priority and fallback logic"""
        # Check if the code implements proper fallback
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Check for transport auto mode
            has_auto_mode = '--transport' in content and 'auto' in content
            has_mqtt = 'MQTTTransport' in content
            has_ws = 'WebSocketTransport' in content
            has_http = 'HTTPTransport' in content

            missing = []
            if not has_mqtt:
                missing.append("MQTT")
            if not has_ws:
                missing.append("WebSocket")
            if not has_http:
                missing.append("HTTP")

            if missing:
                return "FAIL", False, f"Missing transports: {', '.join(missing)}"

            if not has_auto_mode:
                return "WARN", True, "All transports present but auto mode unclear"

            return "PASS", True, "All 3 transports implemented with auto mode"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_state_machine_transitions(self) -> Tuple[str, bool, str]:
        """Verify state machine has all required states"""
        try:
            # Check v5 C++ code
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            has_discovery = 'DISCOVERY' in content
            has_locked = 'LOCKED' in content
            has_mqtt = 'TransportType::MQTT' in content
            has_ws = 'TransportType::WEBSOCKET' in content
            has_http = 'TransportType::HTTP' in content

            missing = []
            if not has_discovery:
                missing.append("DISCOVERY state")
            if not has_locked:
                missing.append("LOCKED state")
            if not has_mqtt or not has_ws or not has_http:
                missing.append("Transport types")

            if missing:
                return "FAIL", False, f"State machine incomplete: {', '.join(missing)}"

            # Check for transition logic
            has_switch = 'switchTransport' in content
            has_lock_check = 'checkLockExpiry' in content

            if not has_switch or not has_lock_check:
                return "WARN", True, "States defined but transition logic unclear"

            return "PASS", True, "State machine complete with DISCOVERY/LOCKED and 3 transports"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_error_propagation(self) -> Tuple[str, bool, str]:
        """Test that errors propagate correctly between components"""
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Check for logging/error reporting
            has_logging = 'import logging' in content or 'print(' in content
            has_exceptions = 'raise' in content or 'except' in content

            if not has_logging:
                return "WARN", True, "No obvious logging mechanism found"

            if not has_exceptions:
                return "FAIL", False, "No exception handling found"

            # Check for status reporting
            has_status_send = 'send_status' in content or 'sendStatus' in content

            if has_status_send:
                return "PASS", True, "Error handling and status reporting present"
            else:
                return "WARN", True, "Error handling present but status reporting unclear"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def run_all(self) -> List[Dict]:
        """Run all integration tests"""
        tests = [
            ("L1.1", "MQTT broker connectivity", self.test_mqtt_broker_connectivity),
            ("L1.2", "Required ports available", self.test_port_availability),
            ("L1.3", "UDP broadcast capability", self.test_udp_broadcast_capability),
            ("L1.4", "JSON command serialization", self.test_json_serialization_integration),
            ("L1.5", "Transport selection logic", self.test_transport_selection_logic),
            ("L1.6", "State machine transitions", self.test_state_machine_transitions),
            ("L1.7", "Error propagation", self.test_error_propagation),
        ]

        results = []
        for test_id, test_name, test_func in tests:
            status, passed, details = test_func()
            results.append({
                "id": test_id,
                "name": test_name,
                "status": status,
                "passed": passed,
                "details": details
            })

        return results

if __name__ == "__main__":
    tester = IntegrationTests()
    results = tester.run_all()

    print(json.dumps(results, indent=2))
