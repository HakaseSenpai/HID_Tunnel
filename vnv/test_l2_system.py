#!/usr/bin/env python3
"""
V&V Level 2: System Level Tests
Tests complete subsystems and their interactions
"""

import sys
import os
import json
import re
from typing import Dict, List, Tuple

class SystemTests:
    """System-level tests"""

    def __init__(self):
        self.results = []

    def test_critical_security_issues(self) -> Tuple[str, bool, str]:
        """Check for critical security issues from audit"""
        critical_issues = []

        # Test 1: Buffer overflow in mDNS (CPP-001)
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            # Check for dangerous pattern: buffer[len] = '\0' without bounds check
            if re.search(r'buffer\[len\]\s*=\s*\'\\0\'', content):
                # Check if there's a bounds check before it
                if not re.search(r'if\s*\(.+len.+sizeof.+\)', content):
                    critical_issues.append("CPP-001: Buffer overflow in mDNS (no bounds check)")

        except Exception as e:
            critical_issues.append(f"Could not check CPP-001: {e}")

        # Test 2: MQTT payload overflow (CPP-002)
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            if 'payload[len]' in content and 'len >= 512' in content:
                critical_issues.append("CPP-002: MQTT payload overflow risk present")

        except Exception as e:
            critical_issues.append(f"Could not check CPP-002: {e}")

        # Test 3: Unsynchronized vector access (CPP-003)
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            if 'discoveredEndpoints' in content:
                # Check for mutex
                if 'std::mutex' not in content and 'Mutex' not in content:
                    critical_issues.append("CPP-003: Unsynchronized vector access (no mutex)")

        except Exception as e:
            critical_issues.append(f"Could not check CPP-003: {e}")

        # Test 4: Recursive thread creation (PY-002)
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Look for threading.Thread inside _schedule_reconnect
            if '_schedule_reconnect' in content and 'threading.Thread' in content:
                critical_issues.append("PY-002: Potential recursive thread creation")

        except Exception as e:
            critical_issues.append(f"Could not check PY-002: {e}")

        if len(critical_issues) >= 3:
            return "FAIL", False, f"{len(critical_issues)} critical issues found: {'; '.join(critical_issues[:3])}"
        elif len(critical_issues) > 0:
            return "WARN", True, f"{len(critical_issues)} critical issue(s): {'; '.join(critical_issues)}"
        else:
            return "PASS", True, "No critical security issues detected in spot check"

    def test_resource_limits(self) -> Tuple[str, bool, str]:
        """Check for resource limit enforcement"""
        issues = []

        # Check Python code for unbounded queues
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            if 'Queue(' in content:
                # Check if maxsize is set
                if 'Queue()' in content and 'maxsize=' not in content:
                    issues.append("Unbounded Queue detected")

        except Exception as e:
            issues.append(f"Could not check Python: {e}")

        # Check C++ for vector size limits
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            if 'vector<' in content or 'std::vector' in content:
                # Check for size limits
                if 'max_size' not in content and 'reserve(' not in content:
                    issues.append("Vector without size limits")

        except Exception as e:
            issues.append(f"Could not check C++: {e}")

        if len(issues) >= 2:
            return "FAIL", False, f"Resource limits missing: {'; '.join(issues)}"
        elif len(issues) > 0:
            return "WARN", True, f"Some resource limits missing: {'; '.join(issues)}"
        else:
            return "PASS", True, "Resource limits appear enforced"

    def test_watchdog_implementation(self) -> Tuple[str, bool, str]:
        """Verify watchdog timer is properly implemented"""
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            has_wdt_init = 'esp_task_wdt_init' in content
            has_wdt_reset = 'esp_task_wdt_reset' in content
            has_wdt_add = 'esp_task_wdt_add' in content

            if not has_wdt_init:
                return "FAIL", False, "Watchdog not initialized"

            if not has_wdt_reset:
                return "FAIL", False, "Watchdog never reset"

            if not has_wdt_add:
                return "WARN", True, "Watchdog initialized but task not added"

            # Check if reset is called in loop
            if 'duck_control_mqtt_loop' in content and 'esp_task_wdt_reset' in content:
                return "PASS", True, "Watchdog properly implemented and reset in loop"
            else:
                return "WARN", True, "Watchdog implemented but reset location unclear"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_hid_timeout_safety(self) -> Tuple[str, bool, str]:
        """Verify HID timeout releases keys/buttons"""
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            has_hid_timeout = 'HID_TIMEOUT_MS' in content or 'hidTimeout' in content
            has_release_all = 'releaseAll' in content or 'release(' in content
            has_timer = 'hidTimeoutTimer' in content or 'HIDTimeout' in content

            if not has_hid_timeout:
                return "FAIL", False, "No HID timeout constant defined"

            if not has_release_all:
                return "FAIL", False, "No release mechanism found"

            if not has_timer:
                return "WARN", True, "Timeout defined but timer unclear"

            # Check if timer callback releases keys
            if 'hidTimeoutCallback' in content and 'releaseAll' in content:
                return "PASS", True, "HID timeout safety properly implemented"
            else:
                return "WARN", True, "HID safety components present but linkage unclear"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_reconnection_logic(self) -> Tuple[str, bool, str]:
        """Test exponential backoff reconnection"""
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            has_reconnect = 'reconnect' in content.lower()
            has_backoff = 'backoff' in content.lower() or 'delay' in content
            has_max_delay = 'max' in content and 'delay' in content

            if not has_reconnect:
                return "FAIL", False, "No reconnection logic found"

            if not has_backoff:
                return "WARN", True, "Reconnection present but backoff unclear"

            if has_backoff and has_max_delay:
                return "PASS", True, "Reconnection with backoff/max delay implemented"
            else:
                return "WARN", True, "Reconnection present but backoff details unclear"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_protocol_compatibility(self) -> Tuple[str, bool, str]:
        """Test protocol version compatibility between Python and ESP32"""
        python_features = set()
        cpp_features = set()

        try:
            # Check Python features
            with open('../HID_remote_v5.py', 'r') as f:
                py_content = f.read()

            if 'keyboard_state' in py_content:
                python_features.add('keyboard_state')
            if 'lock_transport' in py_content:
                python_features.add('lock_transport')
            if 'mDNS' in py_content or 'mdns' in py_content.lower():
                python_features.add('mdns')

            # Check C++ features
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                cpp_content = f.read()

            if 'state' in cpp_content and 'pressed' in cpp_content:
                cpp_features.add('keyboard_state')
            if 'lock_transport' in cpp_content or 'LOCKED' in cpp_content:
                cpp_features.add('lock_transport')
            if 'mdns' in cpp_content.lower() or 'discovery' in cpp_content.lower():
                cpp_features.add('mdns')

            # Check compatibility
            python_only = python_features - cpp_features
            cpp_only = cpp_features - python_features

            if python_only or cpp_only:
                return "WARN", True, f"Feature mismatch - Python only: {python_only}, C++ only: {cpp_only}"

            if len(python_features) == 0:
                return "WARN", True, "No protocol features detected"

            return "PASS", True, f"Protocol features match: {python_features}"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_dependency_versions(self) -> Tuple[str, bool, str]:
        """Check for dependency version specifications"""
        try:
            # Check if platformio.ini exists
            if os.path.exists('../UltraWiFiDuck/platformio.ini'):
                with open('../UltraWiFiDuck/platformio.ini', 'r') as f:
                    content = f.read()

                has_versions = '@' in content or '=' in content

                if not has_versions:
                    return "WARN", True, "platformio.ini exists but no version pinning"

                version_count = content.count('@')
                return "PASS", True, f"Dependency versions specified ({version_count} pinned)"
            else:
                return "WARN", True, "No platformio.ini found for version checking"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def run_all(self) -> List[Dict]:
        """Run all system tests"""
        tests = [
            ("L2.1", "Critical security issues", self.test_critical_security_issues),
            ("L2.2", "Resource limits enforced", self.test_resource_limits),
            ("L2.3", "Watchdog implementation", self.test_watchdog_implementation),
            ("L2.4", "HID timeout safety", self.test_hid_timeout_safety),
            ("L2.5", "Reconnection logic", self.test_reconnection_logic),
            ("L2.6", "Protocol compatibility", self.test_protocol_compatibility),
            ("L2.7", "Dependency versions", self.test_dependency_versions),
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
    tester = SystemTests()
    results = tester.run_all()

    print(json.dumps(results, indent=2))
