"""
Integration tests — require a running server on port 8000.

Run with:
    pytest tests/test_integration.py -v -m integration

All tests marked @pytest.mark.integration — skipped in normal test runs.
"""

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

BASE = "http://localhost:8000"


def _server_available() -> bool:
    try:
        import httpx
        httpx.get(f"{BASE}/health", timeout=3)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.integration

if not _server_available():
    pytest.skip(
        "Server not running on port 8000. Start with: uvicorn src.api.main:app --port 8000",
        allow_module_level=True,
    )

import httpx


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE, timeout=30) as c:
        yield c


@pytest.fixture(scope="module")
def test_file():
    """Create a temp Python file with known issues and clean up after module."""
    f = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8")
    f.write('''\
SECRET = "hardcoded-key-abc"
def login(conn, user):
    q = "SELECT * FROM users WHERE name='" + user + "'"
    conn.cursor().execute(q)
def run(cmd):
    return eval(cmd)
''')
    f.close()
    yield f.name
    os.unlink(f.name)


# ── Health tests ───────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_health_returns_200(client):
    r = client.get("/health")
    assert r.status_code == 200


@pytest.mark.integration
def test_health_has_required_fields(client):
    data = client.get("/health").json()
    for field in ("status", "groq_connected", "neo4j_connected", "uptime_seconds"):
        assert field in data, f"Missing: {field}"


@pytest.mark.integration
def test_health_status_is_valid(client):
    status = client.get("/health").json()["status"]
    assert status in ("healthy", "degraded", "unhealthy")


@pytest.mark.integration
def test_root_returns_links(client):
    data = client.get("/").json()
    assert "docs" in data


# ── Review tests ───────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_post_review_completes(client, test_file):
    r = client.post(
        "/review/",
        json={
            "repo_url":       "https://github.com/example/integration-test",
            "files":          [test_file],
            "max_files":      1,
            "use_memory":     True,
            "use_defect_api": False,
        },
        timeout=300,
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text[:200]}"


@pytest.mark.integration
def test_post_review_has_session_id(client, test_file):
    r = client.post(
        "/review/",
        json={
            "repo_url":       "https://github.com/example/integration-test",
            "files":          [test_file],
            "max_files":      1,
            "use_defect_api": False,
        },
        timeout=300,
    )
    assert r.status_code == 200
    assert r.json()["session_id"] != ""


@pytest.mark.integration
def test_get_completed_review(client, test_file):
    r = client.post(
        "/review/",
        json={
            "repo_url":       "https://github.com/example/integration-test",
            "files":          [test_file],
            "max_files":      1,
            "use_defect_api": False,
        },
        timeout=300,
    )
    assert r.status_code == 200
    sid = r.json()["session_id"]
    r2  = client.get(f"/review/{sid}")
    assert r2.status_code == 200
    assert r2.json()["session_id"] == sid


@pytest.mark.integration
def test_unknown_session_returns_404(client):
    r = client.get("/review/nonexistent-id-xyz")
    assert r.status_code == 404


@pytest.mark.integration
def test_review_list_returns_list(client):
    r = client.get("/review/list")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── SSE streaming tests ────────────────────────────────────────────────────────

@pytest.mark.integration
def test_sse_emits_step_and_done_events(test_file):
    """Connect to /stream/review and verify we get step + done events."""
    current_type = "status"
    step_seen    = False
    done_seen    = False

    url = (
        f"{BASE}/stream/review"
        f"?repo_url=https://github.com/example/sse-test"
        f"&files={test_file}"
        f"&max_files=1"
        f"&use_memory=false"
    )

    with httpx.stream("GET", url, timeout=300) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                try:
                    json.loads(line[5:].strip())
                except Exception:
                    continue
                if current_type == "step":
                    step_seen = True
                elif current_type == "done":
                    done_seen = True
                    break

    assert step_seen, "No 'step' events received from SSE stream"
    assert done_seen, "No 'done' event received from SSE stream"


@pytest.mark.integration
def test_sse_done_has_files_reviewed(test_file):
    """The 'done' event must include files_reviewed count."""
    done_data    = None
    current_type = "status"

    url = (
        f"{BASE}/stream/review"
        f"?repo_url=https://github.com/example/sse-test2"
        f"&files={test_file}"
        f"&max_files=1"
        f"&use_memory=false"
    )

    with httpx.stream("GET", url, timeout=300) as resp:
        for line in resp.iter_lines():
            if line.startswith("event:"):
                current_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and current_type == "done":
                try:
                    done_data = json.loads(line[5:].strip())
                except Exception:
                    pass
                break

    assert done_data is not None, "No done event received"
    assert "files_reviewed" in done_data, f"Missing files_reviewed in: {done_data}"


# ── Memory endpoint tests ──────────────────────────────────────────────────────

@pytest.mark.integration
def test_memory_stats_returns_200(client):
    r = client.get("/memory/stats")
    assert r.status_code == 200


@pytest.mark.integration
def test_memory_stats_has_connected_field(client):
    data = client.get("/memory/stats").json()
    assert "connected" in data


@pytest.mark.integration
def test_memory_patterns_returns_200(client):
    r = client.get(
        "/memory/patterns",
        params={"repo_url": "https://github.com/example/integration-test"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "patterns" in data
    assert "total" in data


@pytest.mark.integration
def test_memory_file_returns_404_for_unknown(client):
    r = client.get(
        "/memory/file",
        params={
            "file_path": "/absolutely/nonexistent/path/file.py",
            "repo_url":  "https://github.com/example/nonexistent",
        },
    )
    assert r.status_code in (404, 503)


# ── GitHub tools tests ─────────────────────────────────────────────────────────

@pytest.mark.integration
def test_github_clone_returns_path():
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not installed")

    from src.tools.github_tools import clone_github_repo, cleanup_github_clone

    result = clone_github_repo("https://github.com/mitsuhiko/click", depth=1)
    assert not result.startswith("ERROR"), f"Clone failed: {result}"
    assert Path(result).exists(), f"Clone dir does not exist: {result}"
    cleanup_github_clone("https://github.com/mitsuhiko/click")


@pytest.mark.integration
def test_github_list_files():
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not installed")

    from src.tools.github_tools import list_github_files, cleanup_github_clone

    result = list_github_files("https://github.com/mitsuhiko/click", max_files=5)
    assert not result.startswith("ERROR"), f"List failed: {result}"
    assert ".py" in result
    cleanup_github_clone("https://github.com/mitsuhiko/click")


@pytest.mark.integration
def test_github_cleanup_removes_directory():
    import shutil
    if not shutil.which("git"):
        pytest.skip("git not installed")

    from src.tools.github_tools import clone_github_repo, cleanup_github_clone, get_active_clones

    url  = "https://github.com/mitsuhiko/click"
    path = clone_github_repo(url, depth=1)
    assert not path.startswith("ERROR")
    p    = Path(path)
    assert p.exists()
    cleanup_github_clone(url)
    assert not p.exists(), "Clone directory still exists after cleanup"
    assert url not in get_active_clones()