#!/usr/bin/env python3
"""
V&V Level 0: Component/Unit Level Tests
Tests individual components and functions in isolation
"""

import sys
import os
import json
import importlib.util
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

class ComponentTests:
    """Component-level unit tests"""

    def __init__(self):
        self.results = []

    def load_python_module(self, filepath: str):
        """Safely load a Python module for testing"""
        try:
            spec = importlib.util.spec_from_file_location("module", filepath)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module, None
        except Exception as e:
            return None, str(e)

    def test_get_local_ip_function(self) -> Tuple[str, bool, str]:
        """Test auto-detect local IP function"""
        try:
            # Load v5
            module, err = self.load_python_module("../HID_remote_v5.py")
            if err:
                return "FAIL", False, f"Failed to load module: {err}"

            if not hasattr(module, 'get_local_ip'):
                return "FAIL", False, "Function get_local_ip not found"

            ip = module.get_local_ip()

            # Validate IP format
            parts = ip.split('.')
            if len(parts) != 4:
                return "FAIL", False, f"Invalid IP format: {ip}"

            for part in parts:
                if not part.isdigit() or not (0 <= int(part) <= 255):
                    return "FAIL", False, f"Invalid IP octet: {part}"

            # Should not be 127.0.0.1 unless fallback
            if ip == "127.0.0.1":
                return "WARN", True, "Fallback IP detected (127.0.0.1)"

            return "PASS", True, f"Valid IP detected: {ip}"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_transport_classes_exist(self) -> Tuple[str, bool, str]:
        """Verify all transport classes are defined"""
        try:
            module, err = self.load_python_module("../HID_remote_v5.py")
            if err:
                return "FAIL", False, f"Failed to load module: {err}"

            required_classes = ['MQTTTransport', 'WebSocketTransport', 'HTTPTransport']
            missing = []

            for cls_name in required_classes:
                if not hasattr(module, cls_name):
                    missing.append(cls_name)

            if missing:
                return "FAIL", False, f"Missing classes: {', '.join(missing)}"

            return "PASS", True, f"All transport classes found: {', '.join(required_classes)}"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_json_payload_handling(self) -> Tuple[str, bool, str]:
        """Test JSON parsing edge cases"""
        import json as json_lib

        test_cases = [
            ('{"key": "value"}', True, "Simple object"),
            ('', False, "Empty string"),
            ('invalid', False, "Invalid JSON"),
            ('{"nested": {"deep": {"value": 123}}}', True, "Nested object"),
            ('[1, 2, 3]', True, "Array"),
            ('{"type": "mouse", "dx": 999999999}', True, "Large numbers"),
        ]

        passed = 0
        failed = 0

        for payload, should_parse, desc in test_cases:
            try:
                json_lib.loads(payload)
                if should_parse:
                    passed += 1
                else:
                    failed += 1
            except:
                if not should_parse:
                    passed += 1
                else:
                    failed += 1

        if failed > 0:
            return "FAIL", False, f"JSON parsing: {passed}/{len(test_cases)} passed"

        return "PASS", True, f"JSON parsing: {passed}/{len(test_cases)} passed"

    def test_buffer_size_constants(self) -> Tuple[str, bool, str]:
        """Verify buffer sizes are safe"""
        # Check if code has proper size limits
        issues = []

        # Check v5 Python
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Look for potential buffer issues
            if 'recv(4096)' in content or 'recv(8192)' in content:
                pass  # Reasonable sizes

            if 'recv(65536)' in content or 'recv(100000)' in content:
                issues.append("Large recv() buffer size detected")

        except Exception as e:
            issues.append(f"Could not check Python: {e}")

        if issues:
            return "WARN", True, "; ".join(issues)

        return "PASS", True, "Buffer sizes appear reasonable"

    def test_thread_safety_primitives(self) -> Tuple[str, bool, str]:
        """Check for thread safety primitives"""
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            has_lock = 'threading.Lock()' in content or 'Lock()' in content
            has_with_lock = 'with self.lock:' in content or 'with lock:' in content

            if not has_lock:
                return "FAIL", False, "No threading.Lock() found"

            if not has_with_lock:
                return "WARN", True, "Locks defined but 'with lock:' pattern not found"

            # Count locks
            lock_count = content.count('threading.Lock()')
            with_count = content.count('with self.lock:') + content.count('with lock:')

            return "PASS", True, f"Thread safety: {lock_count} locks, {with_count} with-statements"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_error_handling_coverage(self) -> Tuple[str, bool, str]:
        """Check error handling patterns"""
        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            bare_except_count = content.count('except:')
            try_count = content.count('try:')
            except_exception_count = content.count('except Exception')

            if bare_except_count > try_count * 0.3:
                return "FAIL", False, f"Too many bare except: {bare_except_count}/{try_count}"

            if try_count == 0:
                return "WARN", True, "No try-except blocks found"

            coverage = (except_exception_count / try_count * 100) if try_count > 0 else 0

            if bare_except_count > 5:
                return "WARN", True, f"Bare except: {bare_except_count}, Typed: {except_exception_count}"

            return "PASS", True, f"Error handling: {try_count} try blocks, {bare_except_count} bare except"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def run_all(self) -> List[Dict]:
        """Run all component tests"""
        tests = [
            ("L0.1", "Auto-detect IP function", self.test_get_local_ip_function),
            ("L0.2", "Transport classes defined", self.test_transport_classes_exist),
            ("L0.3", "JSON payload handling", self.test_json_payload_handling),
            ("L0.4", "Buffer size safety", self.test_buffer_size_constants),
            ("L0.5", "Thread safety primitives", self.test_thread_safety_primitives),
            ("L0.6", "Error handling coverage", self.test_error_handling_coverage),
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
    tester = ComponentTests()
    results = tester.run_all()

    print(json.dumps(results, indent=2))
