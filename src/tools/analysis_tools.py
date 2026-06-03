"""
Analysis Tools
==============
Static analysis tools the agent can call.
Each tool runs a real external analyzer and returns structured results.

Tools:
    run_ruff       — linting + style (fast, comprehensive)
    run_bandit     — security vulnerability scanning
    run_radon      — cyclomatic complexity analysis
    check_imports  — dependency analysis
"""

import ast
import json
import subprocess
from pathlib import Path
from loguru import logger
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import TOOLS, AGENT
from src.tools.registry import Tool, ToolRegistry


def run_ruff(file_path: str) -> str:
    """Run ruff linter on a Python file.

    Returns all violations with line numbers, codes, and descriptions.
    Groups results by category: errors, warnings, style.
    ruff exits 1 when violations are found — this is expected, not an error.

    Args:
        file_path: Path to the Python file to analyse.

    Returns:
        Formatted string of violations, or a clean-pass message, or an error string.
    """
    path = Path(file_path)
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        result = subprocess.run(
            [
                TOOLS["ruff_executable"], "check",
                "--output-format", "json",
                "--no-cache",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=AGENT["tool_timeout_seconds"],
        )

        # ruff exits 1 when violations found — that's expected, not an error
        if result.returncode not in (0, 1):
            return f"ERROR: ruff failed (exit {result.returncode}): {result.stderr[:300]}"

        if not result.stdout.strip():
            return "ruff: No violations found. File passes all checks."

        try:
            violations = json.loads(result.stdout)
        except json.JSONDecodeError:
            return f"ruff output (raw):\n{result.stdout[:2000]}"

        if not violations:
            return "ruff: No violations found."

        lines = [f"ruff found {len(violations)} violation(s) in {path.name}:\n"]
        for v in violations:
            lines.append(
                f"  Line {v.get('location', {}).get('row', '?')}: "
                f"[{v.get('code', '?')}] {v.get('message', '')}  "
                f"({v.get('url', '')})"
            )

        codes = [v.get("code", "") for v in violations]
        lines.append(f"\nSummary: {len(violations)} total violations")
        # Breakdown by first letter of code (E=pycodestyle error, W=warning, F=pyflakes, etc.)
        categories: dict[str, int] = {}
        for code in codes:
            if code:
                prefix = code[0]
                categories[prefix] = categories.get(prefix, 0) + 1
        if categories:
            cat_str = ", ".join(f"{k}x{v}" for k, v in sorted(categories.items()))
            lines.append(f"Categories: {cat_str}")

        return "\n".join(lines)

    except subprocess.TimeoutExpired:
        return f"ERROR: ruff timed out after {AGENT['tool_timeout_seconds']}s"
    except FileNotFoundError:
        return "ERROR: ruff not found. Install with: pip install ruff"
    except Exception as e:
        return f"ERROR running ruff: {e}"


def run_bandit(file_path: str, severity_threshold: str = "LOW") -> str:
    """Run bandit security scanner on a Python file.

    Returns security vulnerabilities with CWE references and severity/confidence
    ratings.  bandit exits 1 when issues are found — this is expected.

    Args:
        file_path: Path to the Python file to scan.
        severity_threshold: Minimum severity level to report — LOW, MEDIUM, or HIGH.

    Returns:
        Formatted string of security issues, a clean-pass message, or an error string.
    """
    path = Path(file_path)
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        result = subprocess.run(
            [
                TOOLS["bandit_executable"],
                "-f", "json",
                "-l",                    # show line numbers
                "-i",                    # show issue text
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=AGENT["tool_timeout_seconds"],
        )

        # bandit exits 1 when issues found — expected
        if result.returncode not in (0, 1):
            return f"ERROR: bandit failed (exit {result.returncode}): {result.stderr[:300]}"

        if not result.stdout.strip():
            return "bandit: No security issues found."

        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            return f"bandit output (raw):\n{result.stdout[:2000]}"

        results = report.get("results", [])
        if not results:
            return "bandit: No security issues found. File passes security scan."

        severity_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
        threshold_level = severity_order.get(severity_threshold.upper(), 0)
        filtered = [
            r for r in results
            if severity_order.get(r.get("issue_severity", "LOW"), 0) >= threshold_level
        ]

        if not filtered:
            return f"bandit: No issues at or above {severity_threshold} severity."

        lines = [f"bandit found {len(filtered)} security issue(s) in {path.name}:\n"]
        for issue in filtered:
            lines.append(
                f"  Line {issue.get('line_number', '?')}: "
                f"[{issue.get('issue_severity', '?')}/{issue.get('issue_confidence', '?')}] "
                f"{issue.get('issue_text', '')}\n"
                f"    Test: {issue.get('test_id', '')} — {issue.get('test_name', '')}\n"
                f"    CWE: {issue.get('issue_cwe', {}).get('id', 'N/A')} — "
                f"{issue.get('issue_cwe', {}).get('link', '')}"
            )

        metrics = report.get("metrics", {}).get("_totals", {})
        lines.append(
            f"\nSeverity breakdown: "
            f"HIGH={metrics.get('SEVERITY.HIGH', 0)} "
            f"MEDIUM={metrics.get('SEVERITY.MEDIUM', 0)} "
            f"LOW={metrics.get('SEVERITY.LOW', 0)}"
        )

        return "\n".join(lines)

    except subprocess.TimeoutExpired:
        return f"ERROR: bandit timed out after {AGENT['tool_timeout_seconds']}s"
    except FileNotFoundError:
        return "ERROR: bandit not found. Install with: pip install bandit"
    except Exception as e:
        return f"ERROR running bandit: {e}"


def run_radon(file_path: str) -> str:
    """Run radon cyclomatic complexity analysis on a Python file.

    Reports complexity per function with letter grades A–F.
    A = simple and easy to test; F = unmaintainable spaghetti.
    Functions graded C or worse are explicitly flagged for attention.

    Args:
        file_path: Path to the Python file to analyse.

    Returns:
        Formatted complexity report string, or an error message.
    """
    path = Path(file_path)
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        result = subprocess.run(
            ["radon", "cc", "-s", "-j", str(path)],
            capture_output=True,
            text=True,
            timeout=AGENT["tool_timeout_seconds"],
        )

        if result.returncode != 0:
            return f"ERROR: radon failed: {result.stderr[:300]}"

        if not result.stdout.strip():
            return "radon: No output produced. File may be empty."

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return f"radon output:\n{result.stdout[:1000]}"

        # radon keys the results by file path string
        file_data = data.get(str(path), [])
        if not file_data:
            return "radon: No functions found or file is empty."

        lines = [f"Cyclomatic complexity for {path.name}:\n"]
        complex_funcs = []

        for item in sorted(file_data, key=lambda x: x.get("complexity", 0), reverse=True):
            grade = item.get("rank", "?")
            complexity = item.get("complexity", 0)
            name = item.get("name", "?")
            lineno = item.get("lineno", "?")

            flag = ""
            if grade in ("D", "E", "F"):
                flag = " ← COMPLEX, needs refactoring"
                complex_funcs.append(name)
            elif grade == "C":
                flag = " ← moderately complex"

            lines.append(
                f"  {name} (line {lineno}): complexity={complexity}, grade={grade}{flag}"
            )

        if complex_funcs:
            lines.append(
                f"\nFunctions needing attention: {', '.join(complex_funcs)}"
            )
        else:
            lines.append("\nAll functions have acceptable complexity (A or B grade).")

        return "\n".join(lines)

    except FileNotFoundError:
        return "ERROR: radon not found. Install with: pip install radon"
    except Exception as e:
        return f"ERROR running radon: {e}"


def check_imports(file_path: str) -> str:
    """Analyse imports in a Python file using AST.

    Reports: stdlib vs third-party vs local imports, wildcard imports
    (bad practice), and high coupling warnings when many third-party
    packages are imported.

    Args:
        file_path: Path to the Python file to inspect.

    Returns:
        Formatted import analysis string, or an error message.
    """
    path = Path(file_path)
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)

        stdlib_modules = {
            "os", "sys", "re", "json", "time", "datetime", "pathlib",
            "typing", "collections", "itertools", "functools", "abc",
            "io", "math", "random", "hashlib", "logging", "threading",
            "subprocess", "shutil", "tempfile", "copy", "enum", "dataclasses",
            "ast", "inspect", "traceback", "warnings", "contextlib",
            "string", "struct", "socket", "select", "signal", "gc",
            "weakref", "pickle", "shelve", "csv", "configparser",
            "argparse", "getopt", "textwrap", "pprint", "reprlib",
            "numbers", "decimal", "fractions", "statistics",
            "unittest", "doctest", "pdb",
        }

        imports = []
        wildcard_imports = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(("import", alias.name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        wildcard_imports.append((module, node.lineno))
                    imports.append(("from", module, node.lineno))

        if not imports and not wildcard_imports:
            return "No imports found in this file."

        stdlib = [
            (t, m, l) for t, m, l in imports
            if m.split(".")[0] in stdlib_modules
        ]
        third_party = [
            (t, m, l) for t, m, l in imports
            if m.split(".")[0] not in stdlib_modules and not m.startswith(".")
        ]
        local = [(t, m, l) for t, m, l in imports if m.startswith(".")]

        lines = [f"Import analysis for {path.name}:\n"]
        lines.append(
            f"  Standard library ({len(stdlib)}): "
            f"{', '.join(sorted(set(m for _, m, _ in stdlib))) or 'none'}"
        )
        lines.append(
            f"  Third-party ({len(third_party)}): "
            f"{', '.join(sorted(set(m.split('.')[0] for _, m, _ in third_party))) or 'none'}"
        )
        lines.append(
            f"  Local/relative ({len(local)}): "
            f"{', '.join(sorted(set(m for _, m, _ in local))) or 'none'}"
        )

        if wildcard_imports:
            lines.append(f"\n  ⚠ Wildcard imports (avoid these):")
            for module, lineno in wildcard_imports:
                lines.append(f"    Line {lineno}: from {module} import *")

        unique_third_party = len(set(m.split(".")[0] for _, m, _ in third_party))
        if unique_third_party > 8:
            lines.append(
                f"\n  ⚠ High coupling: {unique_third_party} distinct third-party dependencies"
            )

        return "\n".join(lines)

    except SyntaxError as e:
        return f"Syntax error in {file_path}: {e}"
    except Exception as e:
        return f"ERROR analyzing imports: {e}"


def register_analysis_tools(registry: ToolRegistry) -> None:
    """Register all static analysis tools into the given registry.

    Args:
        registry: The ToolRegistry instance to register tools into.
    """
    registry.register(Tool(
        name="run_ruff",
        description=(
            "Run ruff linter for style violations, unused imports, and common bugs. "
            "Always run this."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to Python file"},
            },
            "required": ["file_path"],
        },
        func=run_ruff,
        category="analysis",
    ))

    registry.register(Tool(
        name="run_bandit",
        description=(
            "Run bandit security scanner. Always run this — catches SQL injection, "
            "hardcoded passwords, eval() misuse."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to Python file"},
                "severity_threshold": {
                    "type": "string",
                    "description": "LOW/MEDIUM/HIGH (default LOW)",
                },
            },
            "required": ["file_path"],
        },
        func=run_bandit,
        category="analysis",
    ))

    registry.register(Tool(
        name="run_radon",
        description=(
            "Run radon complexity analysis. Use for files over 100 lines or when "
            "ruff flags complexity issues."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to Python file"},
            },
            "required": ["file_path"],
        },
        func=run_radon,
        category="analysis",
    ))

    registry.register(Tool(
        name="check_imports",
        description="Analyze imports for coupling, wildcard imports, and dependency risks.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to Python file"},
            },
            "required": ["file_path"],
        },
        func=check_imports,
        category="analysis",
    ))