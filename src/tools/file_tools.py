"""
File Tools
==========
Tools for reading and navigating code files.
These are the agent's eyes — it can only see code through these tools.
"""

import ast
from pathlib import Path
from typing import Optional
from loguru import logger
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import TOOLS
from src.tools.registry import Tool, ToolRegistry


def read_file(
    file_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """Read a Python file's content, optionally a specific line range.

    Returns content with line numbers prefixed for easy reference.
    Enforces the max file size limit from config.

    Args:
        file_path: Absolute or relative path to the Python file.
        start_line: Optional start line (1-indexed, inclusive).
        end_line: Optional end line (1-indexed, inclusive).

    Returns:
        Numbered file content as a string, or an error message string.
    """
    path = Path(file_path)

    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    if path.suffix != ".py":
        return f"ERROR: Only Python files supported. Got: {path.suffix}"

    size = path.stat().st_size
    if size > TOOLS["max_file_size_bytes"]:
        return (
            f"ERROR: File too large ({size:,} bytes). "
            f"Max: {TOOLS['max_file_size_bytes']:,} bytes"
        )

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        total_lines = len(lines)

        if start_line is not None or end_line is not None:
            s = (start_line or 1) - 1
            e = end_line or total_lines
            lines = lines[s:e]
            header = f"[Lines {start_line or 1}-{end_line or total_lines} of {path.name}]\n"
        else:
            header = f"[{path.name} — {total_lines} lines]\n"

        numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
        return header + numbered

    except Exception as e:
        return f"ERROR reading {file_path}: {e}"


def list_python_files(directory: str, max_files: int = 20) -> str:
    """List all Python files in a directory recursively.

    Excludes test files and common non-source directories such as
    __pycache__, .git, venv, and node_modules.  Returns a formatted
    list with relative paths and file sizes.

    Args:
        directory: Path to the directory to search.
        max_files: Maximum number of files to return (default 20).

    Returns:
        Formatted string listing Python files, or an error message.
    """
    path = Path(directory)
    if not path.exists():
        return f"ERROR: Directory not found: {directory}"

    ignore = {"__pycache__", ".git", "venv", ".venv", "node_modules", "dist", "build"}
    py_files = []

    for f in path.rglob("*.py"):
        if any(part in ignore for part in f.parts):
            continue
        if any(test in f.name.lower() for test in ["test_", "_test"]):
            continue
        py_files.append(f)

    py_files = sorted(py_files)[:max_files]

    if not py_files:
        return f"No Python files found in {directory}"

    lines = [f"Python files in {directory} ({len(py_files)} shown):"]
    for f in py_files:
        size_kb = f.stat().st_size / 1024
        try:
            rel_path = f.relative_to(path)
        except ValueError:
            rel_path = f
        lines.append(f"  {rel_path}  ({size_kb:.1f} KB)")

    return "\n".join(lines)


def get_function_context(file_path: str, function_name: str) -> str:
    """Extract a specific function's source code using AST parsing.

    Useful for deep-diving into a function flagged as complex or risky.
    Returns the function with original line numbers preserved.

    Args:
        file_path: Path to the Python file containing the function.
        function_name: Exact name of the function to extract.

    Returns:
        Numbered source lines for the function, or an error message.
    """
    path = Path(file_path)
    if not path.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        lines = source.splitlines()

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == function_name:
                    start = node.lineno - 1
                    end = node.end_lineno
                    func_lines = lines[start:end]
                    numbered = "\n".join(
                        f"{start+i+1:4d} | {line}" for i, line in enumerate(func_lines)
                    )
                    return (
                        f"Function '{function_name}' "
                        f"(lines {node.lineno}-{node.end_lineno}):\n{numbered}"
                    )

        return f"Function '{function_name}' not found in {file_path}"

    except SyntaxError as e:
        return f"Syntax error in {file_path}: {e}"
    except Exception as e:
        return f"ERROR extracting function: {e}"


def register_file_tools(registry: ToolRegistry) -> None:
    """Register all file tools into the given registry.

    Args:
        registry: The ToolRegistry instance to register tools into.
    """
    registry.register(Tool(
        name="read_file",
        description=(
            "Read a Python file's content with line numbers. "
            "Use this first for any file you review."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the Python file"},
                "start_line": {"type": "integer", "description": "Optional start line (1-indexed)"},
                "end_line": {"type": "integer", "description": "Optional end line (1-indexed)"},
            },
            "required": ["file_path"],
        },
        func=read_file,
        category="file",
    ))

    registry.register(Tool(
        name="list_python_files",
        description="List all Python files in a directory. Use to understand repo structure.",
        parameters={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "description": "Directory path to search"},
                "max_files": {"type": "integer", "description": "Max files to return (default 20)"},
            },
            "required": ["directory"],
        },
        func=list_python_files,
        category="file",
    ))

    registry.register(Tool(
        name="get_function_context",
        description=(
            "Extract a specific function's source code. "
            "Use when you need to deeply inspect one function."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the Python file"},
                "function_name": {"type": "string", "description": "Name of the function to extract"},
            },
            "required": ["file_path", "function_name"],
        },
        func=get_function_context,
        category="file",
    ))