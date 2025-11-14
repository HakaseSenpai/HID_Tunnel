#!/usr/bin/env python3
"""
V&V Level 4: Performance Tests
Tests performance characteristics and stress scenarios
"""

import sys
import os
import json
import re
from typing import Dict, List, Tuple

class PerformanceTests:
    """Performance and stress tests"""

    def __init__(self):
        self.results = []

    def test_hid_rate_limiting(self) -> Tuple[str, bool, str]:
        """Verify HID rate limiting is implemented"""
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            has_interval = 'MIN_HID_INTERVAL' in content or 'HID_INTERVAL' in content
            has_throttle = 'throttle' in content.lower() or 'lastHidTime' in content

            if not has_interval:
                return "FAIL", False, "No HID rate limit constant found"

            if not has_throttle:
                return "WARN", True, "Rate limit defined but throttling unclear"

            # Extract the interval value
            match = re.search(r'MIN_HID_INTERVAL.*?=\s*(\d+)', content)
            if match:
                interval_ms = int(match.group(1))
                rate_hz = 1000 / interval_ms if interval_ms > 0 else 0

                if interval_ms < 10:
                    return "WARN", True, f"Very fast HID rate: {rate_hz:.1f} Hz (may overwhelm device)"

                if interval_ms > 100:
                    return "WARN", True, f"Slow HID rate: {rate_hz:.1f} Hz (may feel laggy)"

                return "PASS", True, f"HID rate limited to {rate_hz:.1f} Hz ({interval_ms}ms interval)"
            else:
                return "WARN", True, "Rate limiting present but value not extracted"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_memory_allocation_patterns(self) -> Tuple[str, bool, str]:
        """Check for efficient memory usage patterns"""
        issues = []

        # Check Python code
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Look for potential memory issues
            if 'list()' in content or '[]' in content:
                # Check if lists grow unbounded
                if 'append(' in content:
                    # This is normal, but check for size limits
                    if 'maxsize' not in content and 'max_len' not in content:
                        issues.append("Python: Lists may grow unbounded")

        except Exception as e:
            issues.append(f"Python check failed: {e}")

        # Check C++ code
        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            # Look for heap allocations
            if 'new ' in content and 'delete' not in content:
                issues.append("C++: Possible memory leak (new without delete)")

            if 'malloc' in content and 'free' not in content:
                issues.append("C++: Possible memory leak (malloc without free)")

        except Exception as e:
            issues.append(f"C++ check failed: {e}")

        if len(issues) >= 2:
            return "FAIL", False, f"Memory issues: {'; '.join(issues)}"
        elif len(issues) > 0:
            return "WARN", True, f"Potential memory concern: {'; '.join(issues)}"
        else:
            return "PASS", True, "Memory allocation patterns appear safe"

    def test_network_buffer_sizes(self) -> Tuple[str, bool, str]:
        """Check network buffer sizes are appropriate"""
        try:
            findings = []

            # Check Python
            with open('../HID_remote_v5.py', 'r') as f:
                py_content = f.read()

            recv_sizes = re.findall(r'recv\((\d+)\)', py_content)
            if recv_sizes:
                sizes = [int(s) for s in recv_sizes]
                max_size = max(sizes)
                if max_size > 65536:
                    findings.append(f"Python: Large recv buffer {max_size}")
                elif max_size < 1024:
                    findings.append(f"Python: Small recv buffer {max_size}")

            # Check C++
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                cpp_content = f.read()

            buffers = re.findall(r'char\s+\w+\[(\d+)\]', cpp_content)
            if buffers:
                sizes = [int(s) for s in buffers]
                max_buf = max(sizes)
                if max_buf > 4096:
                    findings.append(f"C++: Large stack buffer {max_buf}")

            if findings:
                return "WARN", True, "; ".join(findings)

            return "PASS", True, "Network buffer sizes appropriate"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_blocking_operations(self) -> Tuple[str, bool, str]:
        """Identify blocking operations in critical paths"""
        blocking_issues = []

        try:
            # Check for blocking HTTP in main loop
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            if 'httpClient.GET()' in content:
                # Check if it's in the main loop
                if 'duck_control_mqtt_loop' in content and 'GET()' in content:
                    blocking_issues.append("HTTP GET in main loop (25s block)")

            if 'delay(' in content:
                delays = re.findall(r'delay\((\d+)\)', content)
                long_delays = [int(d) for d in delays if int(d) > 1000]
                if long_delays:
                    blocking_issues.append(f"Long delays in code: {long_delays}ms")

        except Exception as e:
            blocking_issues.append(f"C++ check failed: {e}")

        try:
            # Check Python for blocking operations
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            if 'queue.get(' in content:
                # Check if timeout is set
                if 'timeout=' in content:
                    timeouts = re.findall(r'timeout=(\d+)', content)
                    long_timeouts = [int(t) for t in timeouts if int(t) > 30]
                    if long_timeouts:
                        blocking_issues.append(f"Long queue timeouts: {long_timeouts}s")

        except Exception as e:
            blocking_issues.append(f"Python check failed: {e}")

        if len(blocking_issues) >= 2:
            return "FAIL", False, f"Multiple blocking ops: {'; '.join(blocking_issues)}"
        elif len(blocking_issues) > 0:
            return "WARN", True, f"Blocking operation detected: {'; '.join(blocking_issues)}"
        else:
            return "PASS", True, "No critical blocking operations detected"

    def test_reconnection_backoff(self) -> Tuple[str, bool, str]:
        """Test exponential backoff parameters"""
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Look for backoff implementation
            has_min_delay = re.search(r'(min.*delay|initial.*delay).*=\s*(\d+)', content, re.I)
            has_max_delay = re.search(r'max.*delay.*=\s*(\d+)', content, re.I)

            if not has_min_delay or not has_max_delay:
                return "WARN", True, "Backoff parameters not clearly defined"

            min_val = int(has_min_delay.group(2)) if has_min_delay else 0
            max_val = int(has_max_delay.group(2)) if has_max_delay else 0

            if min_val < 1:
                return "WARN", True, f"Min backoff too small: {min_val}s"

            if max_val > 300:
                return "WARN", True, f"Max backoff very large: {max_val}s (may appear frozen)"

            if max_val < 10:
                return "WARN", True, f"Max backoff too small: {max_val}s (may overwhelm server)"

            return "PASS", True, f"Backoff range reasonable: {min_val}s to {max_val}s"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_thread_count_limits(self) -> Tuple[str, bool, str]:
        """Check for thread count limits"""
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            thread_creates = content.count('threading.Thread(')

            if thread_creates > 20:
                return "WARN", True, f"Many thread creations: {thread_creates}"

            if thread_creates == 0:
                return "WARN", True, "No threading.Thread() calls found"

            # Check for thread pools
            has_pool = 'ThreadPool' in content or 'Executor' in content

            if has_pool:
                return "PASS", True, f"Thread pool used ({thread_creates} Thread() calls for comparison)"

            return "PASS", True, f"{thread_creates} thread creation points (no pool)"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_qos_settings(self) -> Tuple[str, bool, str]:
        """Verify MQTT QoS settings are optimized"""
        try:
            # Check both Python and C++ for QoS
            findings = []

            with open('../HID_remote_v5.py', 'r') as f:
                py_content = f.read()

            # Look for QoS settings
            if 'qos' in py_content.lower():
                # Mouse should be QoS 0, keyboard QoS 1
                if 'mouse' in py_content and 'qos=0' in py_content:
                    findings.append("Python: Mouse QoS 0 ✓")
                if 'key' in py_content and 'qos=1' in py_content:
                    findings.append("Python: Key QoS 1 ✓")

            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                cpp_content = f.read()

            if 'subscribe' in cpp_content:
                # Check QoS in subscribe calls
                mouse_qos = re.search(r'mouse.*?,\s*(\d)', cpp_content)
                key_qos = re.search(r'key.*?,\s*(\d)', cpp_content)

                if mouse_qos and int(mouse_qos.group(1)) == 0:
                    findings.append("C++: Mouse QoS 0 ✓")
                if key_qos and int(key_qos.group(1)) == 1:
                    findings.append("C++: Key QoS 1 ✓")

            if len(findings) >= 2:
                return "PASS", True, "; ".join(findings)
            elif findings:
                return "WARN", True, f"Partial QoS optimization: {'; '.join(findings)}"
            else:
                return "WARN", True, "QoS settings not clearly optimized"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def run_all(self) -> List[Dict]:
        """Run all performance tests"""
        tests = [
            ("L4.1", "HID rate limiting", self.test_hid_rate_limiting),
            ("L4.2", "Memory allocation patterns", self.test_memory_allocation_patterns),
            ("L4.3", "Network buffer sizes", self.test_network_buffer_sizes),
            ("L4.4", "Blocking operations", self.test_blocking_operations),
            ("L4.5", "Reconnection backoff", self.test_reconnection_backoff),
            ("L4.6", "Thread count limits", self.test_thread_count_limits),
            ("L4.7", "QoS settings", self.test_qos_settings),
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
    tester = PerformanceTests()
    results = tester.run_all()

    print(json.dumps(results, indent=2))
