#!/usr/bin/env python3
"""
V&V Master Test Runner
Executes all test levels (L0-L5) and generates comprehensive report
"""

import sys
import os
import json
import subprocess
from datetime import datetime
from typing import Dict, List

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

class VNVTestRunner:
    """Master test runner for all V&V levels"""

    def __init__(self):
        self.results = {
            "L0": [],
            "L1": [],
            "L2": [],
            "L3": [],
            "L4": [],
            "L5": []
        }
        self.test_scripts = {
            "L0": "test_l0_component.py",
            "L1": "test_l1_integration.py",
            "L2": "test_l2_system.py",
            "L3": "test_l3_e2e.py",
            "L4": "test_l4_performance.py",
            "L5": "test_l5_security.py",
        }

    def run_test_level(self, level: str) -> List[Dict]:
        """Run a specific test level"""
        script_path = os.path.join(os.path.dirname(__file__), self.test_scripts[level])

        try:
            result = subprocess.run(
                [sys.executable, script_path],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                return [{
                    "id": f"{level}.ERROR",
                    "name": f"{level} Test Suite Failed",
                    "status": "FAIL",
                    "passed": False,
                    "details": f"Error: {result.stderr}"
                }]

        except subprocess.TimeoutExpired:
            return [{
                "id": f"{level}.TIMEOUT",
                "name": f"{level} Test Suite Timeout",
                "status": "FAIL",
                "passed": False,
                "details": "Test execution exceeded 30 seconds"
            }]
        except Exception as e:
            return [{
                "id": f"{level}.EXCEPTION",
                "name": f"{level} Test Suite Exception",
                "status": "FAIL",
                "passed": False,
                "details": str(e)
            }]

    def run_all_tests(self):
        """Execute all test levels"""
        print("╔════════════════════════════════════════════════════════════════╗")
        print("║        HID Tunnel V&V Test Suite - Running All Levels         ║")
        print("╚════════════════════════════════════════════════════════════════╝")
        print()

        for level in ["L0", "L1", "L2", "L3", "L4", "L5"]:
            print(f"Running {level} tests...", end=" ", flush=True)
            results = self.run_test_level(level)
            self.results[level] = results

            # Count pass/fail
            passed = sum(1 for r in results if r['status'] == 'PASS')
            total = len(results)
            print(f"{passed}/{total} passed")

        print()

    def generate_summary(self) -> Dict:
        """Generate test summary statistics"""
        summary = {
            "total_tests": 0,
            "passed": 0,
            "failed": 0,
            "warned": 0,
            "pass_rate": 0.0
        }

        for level_results in self.results.values():
            for result in level_results:
                summary["total_tests"] += 1
                if result["status"] == "PASS":
                    summary["passed"] += 1
                elif result["status"] == "WARN":
                    summary["warned"] += 1
                else:
                    summary["failed"] += 1

        if summary["total_tests"] > 0:
            summary["pass_rate"] = (summary["passed"] / summary["total_tests"]) * 100

        return summary

    def generate_html_report(self, output_path: str):
        """Generate colorful HTML report"""
        summary = self.generate_summary()

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>HID Tunnel V&V Test Report</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            text-align: center;
            margin-bottom: 30px;
        }}
        .summary {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 30px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }}
        .stat-card {{
            background: #f8f9fa;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-value {{
            font-size: 36px;
            font-weight: bold;
            margin: 10px 0;
        }}
        .stat-label {{
            color: #666;
            font-size: 14px;
        }}
        .level-section {{
            background: white;
            margin-bottom: 20px;
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .level-header {{
            padding: 15px 20px;
            font-weight: bold;
            font-size: 18px;
            color: white;
        }}
        .level-L0 {{ background: linear-gradient(135deg, #6a11cb 0%, #2575fc 100%); }}
        .level-L1 {{ background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }}
        .level-L2 {{ background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }}
        .level-L3 {{ background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%); }}
        .level-L4 {{ background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }}
        .level-L5 {{ background: linear-gradient(135deg, #30cfd0 0%, #330867 100%); }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th {{
            background: #f8f9fa;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid #dee2e6;
        }}
        td {{
            padding: 12px;
            border-bottom: 1px solid #dee2e6;
        }}
        tr:hover {{
            background-color: #f8f9fa;
        }}
        .status {{
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-weight: bold;
            font-size: 12px;
        }}
        .status-PASS {{
            background-color: #d4edda;
            color: #155724;
        }}
        .status-FAIL {{
            background-color: #f8d7da;
            color: #721c24;
        }}
        .status-WARN {{
            background-color: #fff3cd;
            color: #856404;
        }}
        .details {{
            color: #666;
            font-size: 13px;
        }}
        .timestamp {{
            text-align: center;
            color: #999;
            margin-top: 30px;
            font-size: 12px;
        }}
        .pass-rate {{
            font-size: 48px;
            font-weight: bold;
        }}
        .pass-rate.good {{ color: #28a745; }}
        .pass-rate.medium {{ color: #ffc107; }}
        .pass-rate.poor {{ color: #dc3545; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🔬 HID Tunnel V&V Test Report</h1>
        <p>Comprehensive Verification & Validation Matrix (L0-L5)</p>
    </div>

    <div class="summary">
        <h2>Executive Summary</h2>
        <div class="summary-grid">
            <div class="stat-card">
                <div class="stat-label">Total Tests</div>
                <div class="stat-value">{summary['total_tests']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Passed</div>
                <div class="stat-value" style="color: #28a745;">{summary['passed']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Warnings</div>
                <div class="stat-value" style="color: #ffc107;">{summary['warned']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Failed</div>
                <div class="stat-value" style="color: #dc3545;">{summary['failed']}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Pass Rate</div>
                <div class="stat-value pass-rate {'good' if summary['pass_rate'] >= 80 else 'medium' if summary['pass_rate'] >= 60 else 'poor'}">{summary['pass_rate']:.1f}%</div>
            </div>
        </div>
    </div>
"""

        # Generate tables for each level
        level_descriptions = {
            "L0": "Component/Unit Level - Individual functions and components",
            "L1": "Integration Level - Component interactions and interfaces",
            "L2": "System Level - Complete subsystems and critical paths",
            "L3": "End-to-End Level - User workflows and documentation",
            "L4": "Performance Level - Efficiency and resource usage",
            "L5": "Security/Robustness Level - Vulnerabilities and edge cases"
        }

        for level in ["L0", "L1", "L2", "L3", "L4", "L5"]:
            html += f"""
    <div class="level-section">
        <div class="level-header level-{level}">
            {level}: {level_descriptions[level]}
        </div>
        <table>
            <thead>
                <tr>
                    <th style="width: 10%">Test ID</th>
                    <th style="width: 30%">Test Name</th>
                    <th style="width: 10%">Status</th>
                    <th style="width: 50%">Details</th>
                </tr>
            </thead>
            <tbody>
"""

            for result in self.results[level]:
                status_class = f"status-{result['status']}"
                html += f"""
                <tr>
                    <td><strong>{result['id']}</strong></td>
                    <td>{result['name']}</td>
                    <td><span class="status {status_class}">{result['status']}</span></td>
                    <td class="details">{result['details']}</td>
                </tr>
"""

            html += """
            </tbody>
        </table>
    </div>
"""

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html += f"""
    <div class="timestamp">
        Report generated: {timestamp}
    </div>
</body>
</html>
"""

        with open(output_path, 'w') as f:
            f.write(html)

        print(f"✅ HTML report generated: {output_path}")

    def generate_markdown_report(self, output_path: str):
        """Generate markdown report with emoji indicators"""
        summary = self.generate_summary()

        md = f"""# 🔬 HID Tunnel V&V Test Report

**Comprehensive Verification & Validation Matrix (L0-L5)**

Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

---

## 📊 Executive Summary

| Metric | Value |
|--------|-------|
| **Total Tests** | {summary['total_tests']} |
| **✅ Passed** | {summary['passed']} |
| **⚠️  Warnings** | {summary['warned']} |
| **❌ Failed** | {summary['failed']} |
| **Pass Rate** | {summary['pass_rate']:.1f}% |

---

"""

        level_descriptions = {
            "L0": "Component/Unit Level - Individual functions and components",
            "L1": "Integration Level - Component interactions and interfaces",
            "L2": "System Level - Complete subsystems and critical paths",
            "L3": "End-to-End Level - User workflows and documentation",
            "L4": "Performance Level - Efficiency and resource usage",
            "L5": "Security/Robustness Level - Vulnerabilities and edge cases"
        }

        for level in ["L0", "L1", "L2", "L3", "L4", "L5"]:
            passed = sum(1 for r in self.results[level] if r['status'] == 'PASS')
            total = len(self.results[level])

            md += f"""## {level}: {level_descriptions[level]}

**Results: {passed}/{total} passed**

| ID | Test Name | Status | Details |
|----|-----------|--------|---------|
"""

            for result in self.results[level]:
                status_emoji = "✅" if result['status'] == 'PASS' else "⚠️" if result['status'] == 'WARN' else "❌"
                md += f"| {result['id']} | {result['name']} | {status_emoji} {result['status']} | {result['details']} |\n"

            md += "\n---\n\n"

        # Add recommendations based on results
        md += """## 🎯 Recommendations

"""

        if summary['failed'] > 0:
            md += f"- **CRITICAL**: {summary['failed']} test(s) failed. Review failures immediately.\n"

        if summary['warned'] > 0:
            md += f"- **WARNING**: {summary['warned']} test(s) have warnings. Address these before production.\n"

        if summary['pass_rate'] < 80:
            md += f"- **QUALITY**: Pass rate is {summary['pass_rate']:.1f}%. Target is 80%+.\n"

        if summary['pass_rate'] >= 95:
            md += f"- **EXCELLENT**: Pass rate is {summary['pass_rate']:.1f}%. Code quality is high.\n"

        md += """
---

## 📝 Test Level Descriptions

- **L0 (Component)**: Tests individual functions, classes, and modules in isolation
- **L1 (Integration)**: Tests interactions between components and external services
- **L2 (System)**: Tests complete subsystems and critical security issues
- **L3 (End-to-End)**: Tests complete user workflows and documentation
- **L4 (Performance)**: Tests efficiency, resource usage, and scalability
- **L5 (Security)**: Tests security vulnerabilities, robustness, and edge cases

---

*This report was automatically generated by the HID Tunnel V&V Test Suite*
"""

        with open(output_path, 'w') as f:
            f.write(md)

        print(f"✅ Markdown report generated: {output_path}")

    def generate_json_report(self, output_path: str):
        """Generate JSON report for programmatic access"""
        report = {
            "timestamp": datetime.now().isoformat(),
            "summary": self.generate_summary(),
            "results": self.results
        }

        with open(output_path, 'w') as f:
            json.dump(report, f, indent=2)

        print(f"✅ JSON report generated: {output_path}")

def main():
    runner = VNVTestRunner()
    runner.run_all_tests()

    # Generate reports
    print("\nGenerating reports...")
    runner.generate_html_report("../vnv_test_report.html")
    runner.generate_markdown_report("../VNV_TEST_REPORT.md")
    runner.generate_json_report("../vnv_test_results.json")

    # Print summary
    summary = runner.generate_summary()
    print("\n" + "="*64)
    print("FINAL RESULTS")
    print("="*64)
    print(f"Total Tests:  {summary['total_tests']}")
    print(f"✅ Passed:     {summary['passed']}")
    print(f"⚠️  Warnings:   {summary['warned']}")
    print(f"❌ Failed:     {summary['failed']}")
    print(f"Pass Rate:    {summary['pass_rate']:.1f}%")
    print("="*64)

    # Return exit code based on failures
    return 0 if summary['failed'] == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
