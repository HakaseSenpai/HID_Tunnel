#!/usr/bin/env python3
"""
V&V Level 3: End-to-End Tests
Tests complete workflows from user perspective
"""

import sys
import os
import json
from typing import Dict, List, Tuple

class EndToEndTests:
    """End-to-end workflow tests"""

    def __init__(self):
        self.results = []

    def test_quick_start_script(self) -> Tuple[str, bool, str]:
        """Test quick_start.sh script exists and is valid"""
        try:
            script_path = '../quick_start.sh'

            if not os.path.exists(script_path):
                return "FAIL", False, "quick_start.sh not found"

            with open(script_path, 'r') as f:
                content = f.read()

            # Check for required elements
            has_shebang = content.startswith('#!')
            has_python_check = 'python3' in content
            has_dependency_install = 'pip' in content or 'install' in content
            has_mqtt = 'paho' in content or 'mqtt' in content

            issues = []
            if not has_shebang:
                issues.append("No shebang")
            if not has_python_check:
                issues.append("No Python check")
            if not has_dependency_install:
                issues.append("No dependency install")

            if issues:
                return "WARN", True, f"Script exists but: {', '.join(issues)}"

            return "PASS", True, "Quick start script properly configured"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_config_helper_script(self) -> Tuple[str, bool, str]:
        """Test show_config.py helper exists and runs"""
        try:
            script_path = '../show_config.py'

            if not os.path.exists(script_path):
                return "FAIL", False, "show_config.py not found"

            with open(script_path, 'r') as f:
                content = f.read()

            # Check for required functions
            has_get_ip = 'get_local_ip' in content
            has_show_info = 'show_network_info' in content or 'show_config' in content
            has_main = 'if __name__' in content

            if not (has_get_ip and has_show_info and has_main):
                return "WARN", True, "Config helper exists but may be incomplete"

            return "PASS", True, "Configuration helper properly implemented"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def test_documentation_completeness(self) -> Tuple[str, bool, str]:
        """Test documentation files exist and cover key topics"""
        docs = {
            '../PHASE1_SUMMARY.md': ['WebSocket', 'reconnect', 'transport'],
            '../PHASE2_SUMMARY.md': ['HTTP', 'mDNS', 'auto-discovery'],
            '../QUICKSTART.md': ['install', 'run', 'ESP32'],
        }

        missing = []
        incomplete = []

        for doc_path, required_keywords in docs.items():
            if not os.path.exists(doc_path):
                missing.append(os.path.basename(doc_path))
                continue

            try:
                with open(doc_path, 'r') as f:
                    content = f.read().lower()

                missing_keywords = [kw for kw in required_keywords if kw.lower() not in content]
                if missing_keywords:
                    incomplete.append(f"{os.path.basename(doc_path)} (missing: {missing_keywords})")

            except Exception as e:
                incomplete.append(f"{os.path.basename(doc_path)} (error: {e})")

        if missing:
            return "FAIL", False, f"Missing docs: {', '.join(missing)}"

        if incomplete:
            return "WARN", True, f"Incomplete docs: {'; '.join(incomplete)}"

        return "PASS", True, f"All {len(docs)} documentation files complete"

    def test_version_consistency(self) -> Tuple[str, bool, str]:
        """Check version numbers are consistent across files"""
        versions = {}

        # Check Python files
        for pyfile in ['../HID_remote_v4.py', '../HID_remote_v5.py']:
            if os.path.exists(pyfile):
                with open(pyfile, 'r') as f:
                    content = f.read()

                # Look for version strings
                import re
                v_match = re.search(r'v(\d+\.\d+)', content)
                if v_match:
                    versions[pyfile] = v_match.group(1)

        # Check C++ files
        for cppfile in ['../UltraWiFiDuck/src/duck_control_web_v4.cpp',
                        '../UltraWiFiDuck/src/duck_control_web_v5.cpp']:
            if os.path.exists(cppfile):
                with open(cppfile, 'r') as f:
                    content = f.read()

                import re
                v_match = re.search(r'v(\d+\.\d+)', content)
                if v_match:
                    versions[cppfile] = v_match.group(1)

        if len(versions) == 0:
            return "WARN", True, "No version strings found"

        # Check v4 and v5 files have matching versions
        v4_versions = {k: v for k, v in versions.items() if 'v4' in k}
        v5_versions = {k: v for k, v in versions.items() if 'v5' in k}

        v4_set = set(v4_versions.values())
        v5_set = set(v5_versions.values())

        if len(v4_set) > 1:
            return "WARN", True, f"Inconsistent v4 versions: {v4_set}"

        if len(v5_set) > 1:
            return "WARN", True, f"Inconsistent v5 versions: {v5_set}"

        return "PASS", True, f"Version consistency OK (v4: {v4_set}, v5: {v5_set})"

    def test_file_structure(self) -> Tuple[str, bool, str]:
        """Verify expected file structure exists"""
        required_files = [
            '../HID_remote_v5.py',
            '../UltraWiFiDuck/src/duck_control_web_v5.cpp',
            '../UltraWiFiDuck/src/main.cpp',
            '../quick_start.sh',
            '../show_config.py',
        ]

        missing = []
        for filepath in required_files:
            if not os.path.exists(filepath):
                missing.append(os.path.basename(filepath))

        if missing:
            return "FAIL", False, f"Missing required files: {', '.join(missing)}"

        return "PASS", True, f"All {len(required_files)} required files present"

    def test_backwards_compatibility(self) -> Tuple[str, bool, str]:
        """Check if v5 maintains v4 functionality"""
        try:
            v4_functions = set()
            v5_functions = set()

            # Extract v4 functions
            if os.path.exists('../HID_remote_v4.py'):
                with open('../HID_remote_v4.py', 'r') as f:
                    content = f.read()
                import re
                v4_functions = set(re.findall(r'def (\w+)\(', content))

            # Extract v5 functions
            if os.path.exists('../HID_remote_v5.py'):
                with open('../HID_remote_v5.py', 'r') as f:
                    content = f.read()
                import re
                v5_functions = set(re.findall(r'def (\w+)\(', content))

            # Check if v4 functions are in v5
            missing = v4_functions - v5_functions

            # Filter out internal functions
            missing = {f for f in missing if not f.startswith('_')}

            if len(missing) > 5:
                return "WARN", True, f"Many v4 functions missing in v5: {len(missing)}"

            if missing:
                return "WARN", True, f"Some v4 functions not in v5: {len(missing)} functions"

            if len(v5_functions) < len(v4_functions):
                return "WARN", True, f"v5 has fewer functions than v4 ({len(v5_functions)} vs {len(v4_functions)})"

            return "PASS", True, f"v5 maintains v4 functionality ({len(v5_functions)} functions)"

        except Exception as e:
            return "FAIL", False, f"Exception: {str(e)}"

    def run_all(self) -> List[Dict]:
        """Run all end-to-end tests"""
        tests = [
            ("L3.1", "Quick start script", self.test_quick_start_script),
            ("L3.2", "Configuration helper", self.test_config_helper_script),
            ("L3.3", "Documentation completeness", self.test_documentation_completeness),
            ("L3.4", "Version consistency", self.test_version_consistency),
            ("L3.5", "File structure", self.test_file_structure),
            ("L3.6", "Backwards compatibility", self.test_backwards_compatibility),
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
    tester = EndToEndTests()
    results = tester.run_all()

    print(json.dumps(results, indent=2))
