"""
GitHub Tools
============
Lets the agent review real GitHub repositories by cloning them to a
temporary directory, listing Python files, and cleaning up afterwards.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from loguru import logger
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.tools.registry import Tool, ToolRegistry

_active_clones: dict[str, Path] = {}


def clone_github_repo(repo_url: str, depth: int = 100) -> str:
    """Clone a GitHub repository to a temporary directory.

    Uses --depth for a shallow clone. Caches path in _active_clones.

    Args:
        repo_url: GitHub HTTPS URL e.g. https://github.com/pallets/flask
        depth: Shallow clone depth (default 100)

    Returns:
        Absolute path string to cloned directory, or ERROR: string.
    """
    if not repo_url.startswith("https://github.com"):
        return f"ERROR: Only https://github.com URLs supported. Got: {repo_url}"

    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")

    if repo_url in _active_clones and _active_clones[repo_url].exists():
        return str(_active_clones[repo_url])

    try:
        clone_dir = Path(tempfile.mkdtemp(prefix=f"cra_github_{repo_name}_"))
        logger.info(f"Cloning {repo_url} (depth={depth}) to {clone_dir}")

        result = subprocess.run(
            ["git", "clone", f"--depth={depth}", "--no-tags",
             "--single-branch", repo_url, str(clone_dir)],
            capture_output=True, text=True, timeout=120,
        )

        if result.returncode != 0:
            shutil.rmtree(clone_dir, ignore_errors=True)
            return f"ERROR: git clone failed (exit {result.returncode}): {result.stderr[:300]}"

        _active_clones[repo_url] = clone_dir
        py_count = len(list(clone_dir.rglob("*.py")))
        logger.success(f"Cloned {repo_name}: {py_count} Python files")
        return str(clone_dir)

    except subprocess.TimeoutExpired:
        return "ERROR: Clone timed out after 120s."
    except FileNotFoundError:
        return "ERROR: git not found. Install Git: https://git-scm.com/download/win"
    except Exception as e:
        return f"ERROR cloning repo: {e}"


def list_github_files(repo_url: str, max_files: int = 20) -> str:
    """List Python files in a cloned GitHub repo (clones first if needed).

    Args:
        repo_url: GitHub HTTPS URL
        max_files: Maximum files to return (default 20)

    Returns:
        Formatted file listing or ERROR: string.
    """
    clone_path = clone_github_repo(repo_url)
    if clone_path.startswith("ERROR"):
        return clone_path
    from src.tools.file_tools import list_python_files
    return list_python_files(clone_path, max_files=max_files)


def get_github_file_path(repo_url: str, relative_path: str) -> str:
    """Resolve the absolute local path of a file inside a cloned repo.

    Args:
        repo_url: GitHub HTTPS URL
        relative_path: Path relative to repo root e.g. src/flask/app.py

    Returns:
        Absolute path string or ERROR: string.
    """
    if repo_url not in _active_clones:
        result = clone_github_repo(repo_url)
        if result.startswith("ERROR"):
            return result

    clone_dir = _active_clones.get(repo_url)
    if clone_dir is None or not clone_dir.exists():
        return f"ERROR: Clone directory missing for {repo_url}. Re-clone."

    full_path = clone_dir / relative_path
    if full_path.exists():
        return str(full_path)

    # Fallback: search by filename only
    matches = list(clone_dir.rglob(Path(relative_path).name))
    if matches:
        return str(matches[0])

    return (
        f"ERROR: File not found: {relative_path}\n"
        f"  Hint: use list_github_files to see available files."
    )


def cleanup_github_clone(repo_url: str) -> str:
    """Delete the local clone for a repo.

    Args:
        repo_url: GitHub URL to clean up

    Returns:
        Confirmation string.
    """
    if repo_url not in _active_clones:
        return f"No active clone found for {repo_url}"
    clone_dir = _active_clones.pop(repo_url)
    try:
        shutil.rmtree(clone_dir, ignore_errors=True)
        return f"Cleaned up clone at {clone_dir}"
    except Exception as e:
        return f"Cleanup failed: {e}"


def cleanup_all_clones() -> None:
    """Delete all active clones. Call at server shutdown."""
    for url in list(_active_clones.keys()):
        cleanup_github_clone(url)


def get_active_clones() -> dict[str, str]:
    """Return snapshot of active clones as {repo_url: local_path}."""
    return {url: str(path) for url, path in _active_clones.items()}


def register_github_tools(registry: ToolRegistry) -> None:
    """Register all GitHub tools into the given ToolRegistry.

    Args:
        registry: ToolRegistry instance to register into.
    """
    registry.register(Tool(
        name="clone_github_repo",
        description=(
            "Clone a GitHub repository to a local temp directory. "
            "Call FIRST when reviewing a GitHub URL. Returns local path."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo_url": {"type": "string", "description": "GitHub HTTPS URL"},
                "depth": {"type": "integer", "description": "Shallow clone depth (default 100)"},
            },
            "required": ["repo_url"],
        },
        func=clone_github_repo,
        category="github",
    ))

    registry.register(Tool(
        name="list_github_files",
        description="List Python files in a GitHub repo (clones first if needed).",
        parameters={
            "type": "object",
            "properties": {
                "repo_url":  {"type": "string", "description": "GitHub HTTPS URL"},
                "max_files": {"type": "integer", "description": "Max files (default 20)"},
            },
            "required": ["repo_url"],
        },
        func=list_github_files,
        category="github",
    ))

    registry.register(Tool(
        name="get_github_file_path",
        description=(
            "Get the absolute local path for a file in a cloned GitHub repo. "
            "Use before read_file, run_ruff, run_bandit on GitHub files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo_url":      {"type": "string", "description": "GitHub HTTPS URL"},
                "relative_path": {"type": "string", "description": "e.g. src/app.py"},
            },
            "required": ["repo_url", "relative_path"],
        },
        func=get_github_file_path,
        category="github",
    ))

    registry.register(Tool(
        name="cleanup_github_clone",
        description="Delete local clone of a GitHub repo. Call when review is complete.",
        parameters={
            "type": "object",
            "properties": {
                "repo_url": {"type": "string", "description": "GitHub URL to clean up"},
            },
            "required": ["repo_url"],
        },
        func=cleanup_github_clone,
        category="github",
    ))