#!/usr/bin/env python3
"""
V&V Level 5: Security/Robustness Tests
Tests security vulnerabilities and edge cases
"""

import sys
import os
import json
import re
from typing import Dict, List, Tuple

class SecurityTests:
    """Security and robustness tests"""

    def __init__(self):
        self.results = []

    def test_input_validation(self) -> Tuple[str, bool, str]:
        """Check for input validation on network data"""
        issues = []

        try:
            # Check Python JSON parsing
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Look for JSON parsing without validation
            if 'json.loads(' in content:
                # Check if there's exception handling
                if 'except' not in content:
                    issues.append("JSON parsing without exception handling")

            # Check for SQL injection possibilities (should be none)
            if 'execute(' in content and '%' in content:
                issues.append("Possible SQL injection vector")

        except Exception as e:
            issues.append(f"Python check failed: {e}")

        try:
            # Check C++ JSON parsing
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            # Check for DeserializationError handling
            if 'deserializeJson' in content:
                if 'DeserializationError' not in content and 'error' not in content:
                    issues.append("C++: JSON parsing without error checking")

        except Exception as e:
            issues.append(f"C++ check failed: {e}")

        if len(issues) >= 2:
            return "FAIL", False, f"Input validation issues: {'; '.join(issues)}"
        elif len(issues) > 0:
            return "WARN", True, f"Input validation concern: {'; '.join(issues)}"
        else:
            return "PASS", True, "Input validation present"

    def test_authentication_mechanisms(self) -> Tuple[str, bool, str]:
        """Check for authentication and authorization"""
        try:
            auth_features = []

            # Check Python
            with open('../HID_remote_v5.py', 'r') as f:
                py_content = f.read()

            if 'auth' in py_content.lower() or 'password' in py_content.lower():
                auth_features.append("Python: Auth references found")

            if 'device_id' in py_content:
                auth_features.append("Python: Device ID verification")

            # Check C++
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                cpp_content = f.read()

            if 'DEVICE_ID' in cpp_content:
                auth_features.append("C++: Device ID constant")

            # Check mDNS authentication
            if 'mdns' in py_content.lower() or 'mdns' in cpp_content.lower():
                # mDNS should have some authentication
                if 'auth' not in py_content.lower() and 'auth' not in cpp_content.lower():
                    return "WARN", True, "mDNS discovery without authentication"

            if len(auth_features) == 0:
                return "FAIL", False, "No authentication mechanisms found"

            if len(auth_features) < 3:
                return "WARN", True, f"Limited authentication: {'; '.join(auth_features)}"

            return "PASS", True, f"Authentication features: {'; '.join(auth_features)}"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_buffer_overflow_protection(self) -> Tuple[str, bool, str]:
        """Check for buffer overflow vulnerabilities"""
        vulnerabilities = []

        try:
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                content = f.read()

            # Look for dangerous patterns
            # Pattern 1: buffer[len] = '\0' without bounds check
            if re.search(r'buffer\[len\]\s*=', content):
                if not re.search(r'if.*len.*<.*sizeof', content):
                    vulnerabilities.append("Buffer indexing without bounds check")

            # Pattern 2: strcpy without length limit
            if 'strcpy(' in content:
                vulnerabilities.append("Unsafe strcpy() usage")

            # Pattern 3: sprintf without length limit
            if 'sprintf(' in content and 'snprintf(' not in content:
                vulnerabilities.append("Unsafe sprintf() without snprintf()")

            # Pattern 4: Gets (extremely dangerous)
            if 'gets(' in content:
                vulnerabilities.append("CRITICAL: gets() function used")

        except Exception as e:
            vulnerabilities.append(f"C++ check failed: {e}")

        if len(vulnerabilities) >= 2:
            return "FAIL", False, f"Buffer overflow risks: {'; '.join(vulnerabilities)}"
        elif len(vulnerabilities) > 0:
            return "WARN", True, f"Potential buffer issue: {'; '.join(vulnerabilities)}"
        else:
            return "PASS", True, "No obvious buffer overflow patterns"

    def test_race_condition_protection(self) -> Tuple[str, bool, str]:
        """Check for race condition protection"""
        issues = []

        try:
            # Check Python
            with open('../HID_remote_v5.py', 'r') as f:
                py_content = f.read()

            if 'threading' in py_content:
                # Should have locks
                if 'Lock()' not in py_content:
                    issues.append("Python: Threading without locks")

                # Check shared state protection
                if 'self.active' in py_content or 'self.connected' in py_content:
                    # These should be protected
                    if 'with self.lock:' not in py_content and 'with lock:' not in py_content:
                        issues.append("Python: Shared state without lock protection")

        except Exception as e:
            issues.append(f"Python check failed: {e}")

        try:
            # Check C++
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                cpp_content = f.read()

            if 'vector<' in cpp_content or 'std::vector' in cpp_content:
                # Vectors should have mutex protection if accessed from multiple contexts
                if 'mutex' not in cpp_content.lower():
                    issues.append("C++: Vector without mutex protection")

        except Exception as e:
            issues.append(f"C++ check failed: {e}")

        if len(issues) >= 2:
            return "FAIL", False, f"Race condition risks: {'; '.join(issues)}"
        elif len(issues) > 0:
            return "WARN", True, f"Potential race condition: {'; '.join(issues)}"
        else:
            return "PASS", True, "Race condition protections appear adequate"

    def test_dos_resilience(self) -> Tuple[str, bool, str]:
        """Check for DoS attack resilience"""
        vulnerabilities = []

        try:
            # Check for unbounded resource consumption
            with open('../HID_remote_v5.py', 'r') as f:
                py_content = f.read()

            # Check for queue size limits
            if 'Queue()' in py_content:
                if 'maxsize=' not in py_content:
                    vulnerabilities.append("Unbounded queue growth")

            # Check for connection limits
            if 'accept()' in py_content:
                # Should have connection limits
                if 'max_conn' not in py_content and 'limit' not in py_content:
                    vulnerabilities.append("No connection limits")

        except Exception as e:
            vulnerabilities.append(f"Python check failed: {e}")

        try:
            # Check C++
            with open('../UltraWiFiDuck/src/duck_control_web_v5.cpp', 'r') as f:
                cpp_content = f.read()

            # Check for message size limits
            if 'len >= ' not in cpp_content:
                vulnerabilities.append("No message size limits in C++")

        except Exception as e:
            vulnerabilities.append(f"C++ check failed: {e}")

        if len(vulnerabilities) >= 3:
            return "FAIL", False, f"DoS vulnerabilities: {'; '.join(vulnerabilities)}"
        elif len(vulnerabilities) > 0:
            return "WARN", True, f"Potential DoS vectors: {'; '.join(vulnerabilities)}"
        else:
            return "PASS", True, "DoS protections present"

    def test_error_information_disclosure(self) -> Tuple[str, bool, str]:
        """Check for information disclosure in error messages"""
        issues = []

        try:
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            # Look for print statements that might leak info
            prints = re.findall(r'print\(["\'].*Exception.*["\']', content)
            if len(prints) > 5:
                issues.append(f"Many exception prints ({len(prints)}) - may leak info")

            # Check for stack traces
            if 'traceback' in content:
                issues.append("Stack traces may leak sensitive info")

        except Exception as e:
            issues.append(f"Python check failed: {e}")

        if len(issues) >= 2:
            return "WARN", True, f"Info disclosure risks: {'; '.join(issues)}"
        elif len(issues) > 0:
            return "WARN", True, f"Potential info disclosure: {'; '.join(issues)}"
        else:
            return "PASS", True, "No obvious information disclosure"

    def test_command_injection(self) -> Tuple[str, bool, str]:
        """Check for command injection vulnerabilities"""
        try:
            # Check Python for shell command execution
            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            dangerous_funcs = ['os.system(', 'subprocess.call(', 'eval(', 'exec(']
            found = []

            for func in dangerous_funcs:
                if func in content:
                    found.append(func.replace('(', ''))

            if found:
                return "FAIL", False, f"Dangerous functions found: {', '.join(found)}"

            return "PASS", True, "No command injection vectors detected"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_crypto_usage(self) -> Tuple[str, bool, str]:
        """Check for encryption/TLS usage"""
        try:
            crypto_features = []

            with open('../HID_remote_v5.py', 'r') as f:
                content = f.read()

            if 'ssl' in content.lower() or 'tls' in content.lower():
                crypto_features.append("SSL/TLS references")

            if 'crypto' in content.lower() or 'cipher' in content.lower():
                crypto_features.append("Crypto library usage")

            # Check for plaintext warnings
            if 'plaintext' in content.lower():
                crypto_features.append("Plaintext warning present")

            if len(crypto_features) == 0:
                return "WARN", True, "No encryption detected - all traffic plaintext"

            return "PASS", True, f"Crypto features: {'; '.join(crypto_features)}"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def run_all(self) -> List[Dict]:
        """Run all security tests"""
        tests = [
            ("L5.1", "Input validation", self.test_input_validation),
            ("L5.2", "Authentication mechanisms", self.test_authentication_mechanisms),
            ("L5.3", "Buffer overflow protection", self.test_buffer_overflow_protection),
            ("L5.4", "Race condition protection", self.test_race_condition_protection),
            ("L5.5", "DoS resilience", self.test_dos_resilience),
            ("L5.6", "Error info disclosure", self.test_error_information_disclosure),
            ("L5.7", "Command injection", self.test_command_injection),
            ("L5.8", "Encryption usage", self.test_crypto_usage),
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
    tester = SecurityTests()
    results = tester.run_all()

    print(json.dumps(results, indent=2))
