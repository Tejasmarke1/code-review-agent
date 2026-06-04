"""
FastAPI endpoint tests using TestClient — no real server, no real LLM or Neo4j.

All external dependencies (GroqClient, ReviewOrchestrator, Neo4jClient) are
replaced with unittest.mock objects so tests run instantly offline.

Run with:  pytest tests/test_api.py -v
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient

# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_mock_state(
    groq_connected: bool = True,
    neo4j_connected: bool = True,
    review_count: int = 0,
) -> MagicMock:
    """Build a mock AppState with configurable connectivity flags."""
    state = MagicMock()
    state.groq_connected  = groq_connected
    state.neo4j_connected = neo4j_connected
    state.is_healthy      = groq_connected
    state.uptime_seconds  = 42.0
    state.review_count    = review_count
    state.orchestrator    = MagicMock() if groq_connected else None
    return state


def _make_mock_session(
    session_id: str = "abc12345",
    repo_url: str   = "https://github.com/test/repo",
    files: int      = 2,
    issues: int     = 3,
) -> MagicMock:
    """Build a minimal mock OrchestratorSession."""
    from datetime import datetime
    from src.agent.state import AgentState, AgentStatus, ReviewIssue, Severity

    # Build real AgentState objects so _session_to_response works
    states = []
    for i in range(files):
        s = AgentState(
            session_id=f"file{i}",
            repo_url=repo_url,
            file_path=f"/tmp/file{i}.py",
            file_content="x = 1\n",
        )
        s.status = AgentStatus.COMPLETED
        s.risk_score = 0.5
        s.risk_label = "MEDIUM"
        s.current_step = 4
        s.final_review = "## Review\nFound issues."
        s.summary = "Summary text."
        if i == 0 and issues > 0:
            s.add_issue(ReviewIssue(
                file_path=s.file_path,
                line_number=10,
                severity=Severity.HIGH,
                category="security",
                title="Hardcoded password",
                description="Password in source.",
                suggestion="Use env vars.",
                source_tool="bandit",
                confidence=0.9,
            ))
        states.append(s)

    session = MagicMock()
    session.session_id            = session_id
    session.repo_url              = repo_url
    session.started_at            = datetime(2025, 1, 1, 12, 0, 0)
    session.completed_at          = datetime(2025, 1, 1, 12, 1, 0)
    session.files_reviewed        = [s.file_path for s in states]
    session.file_states           = states
    session.total_issues          = issues
    session.critical_issues       = 0
    session.high_issues           = 1
    session.medium_issues         = 2
    session.low_issues            = 0
    session.patterns_detected     = 0
    session.total_elapsed_seconds = 45.0
    session.repo_summary          = "# Report\nAll good."
    session.errors                = []
    return session


def _get_test_client(mock_state: MagicMock) -> TestClient:
    """Return a TestClient with AppState fully replaced by mock_state."""
    # Import app fresh so overrides take effect
    from src.api import main as main_module
    from src.api.dependencies import get_app_state

    app = main_module.app
    app.dependency_overrides[get_app_state] = lambda: mock_state
    return TestClient(app, raise_server_exceptions=False)


# ══════════════════════════════════════════════════════════════════════════════
# Health endpoint tests
# ══════════════════════════════════════════════════════════════════════════════

class TestHealthEndpoint:

    def test_health_returns_200(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_schema_fields_present(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        data = client.get("/health").json()
        assert "status" in data
        assert "neo4j_connected" in data
        assert "groq_connected" in data
        assert "uptime_seconds" in data
        assert "total_reviews_this_session" in data

    def test_health_status_healthy_when_both_connected(self):
        state = _make_mock_state(groq_connected=True, neo4j_connected=True)
        client = _get_test_client(state)
        data = client.get("/health").json()
        assert data["status"] == "healthy"

    def test_health_status_degraded_when_neo4j_down(self):
        state = _make_mock_state(groq_connected=True, neo4j_connected=False)
        client = _get_test_client(state)
        data = client.get("/health").json()
        assert data["status"] == "degraded"

    def test_health_status_unhealthy_when_groq_down(self):
        state = _make_mock_state(groq_connected=False, neo4j_connected=False)
        client = _get_test_client(state)
        data = client.get("/health").json()
        assert data["status"] == "unhealthy"

    def test_root_returns_links(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        data = client.get("/").json()
        assert "docs" in data
        assert "health" in data


# ══════════════════════════════════════════════════════════════════════════════
# Review endpoint tests
# ══════════════════════════════════════════════════════════════════════════════

class TestReviewEndpoint:

    def _client_with_mock_review(self, session_id: str = "abc12345") -> TestClient:
        """Return a client whose orchestrator.review_repo returns a mock session."""
        state = _make_mock_state()
        mock_session = _make_mock_session(session_id=session_id)
        state.orchestrator.review_repo.return_value = mock_session
        return _get_test_client(state)

    def test_post_review_returns_200(self):
        client = self._client_with_mock_review()
        r = client.post("/review/", json={
            "repo_url": "https://github.com/test/repo",
            "files": ["/tmp/a.py", "/tmp/b.py"],
            "max_files": 2,
        })
        assert r.status_code == 200

    def test_post_review_response_has_session_id(self):
        client = self._client_with_mock_review("sess-xyz")
        r = client.post("/review/", json={"repo_url": "https://github.com/test/repo"})
        assert r.json()["session_id"] == "sess-xyz"

    def test_post_review_response_schema(self):
        client = self._client_with_mock_review()
        data = client.post("/review/", json={"repo_url": "https://github.com/test/repo"}).json()
        for field in ("session_id", "repo_url", "files_reviewed", "total_issues",
                      "file_results", "repo_summary", "errors"):
            assert field in data, f"Missing field: {field}"

    def test_post_review_503_when_groq_unavailable(self):
        state = _make_mock_state(groq_connected=False)
        client = _get_test_client(state)
        r = client.post("/review/", json={"repo_url": "https://github.com/test/repo"})
        assert r.status_code == 503
        assert "Groq" in r.json()["detail"]

    def test_post_review_503_when_orchestrator_none(self):
        state = _make_mock_state(groq_connected=True)
        state.orchestrator = None
        client = _get_test_client(state)
        r = client.post("/review/", json={"repo_url": "https://github.com/test/repo"})
        assert r.status_code == 503

    def test_post_review_validates_repo_url(self):
        client = self._client_with_mock_review()
        r = client.post("/review/", json={"repo_url": "not-a-url"})
        assert r.status_code == 422  # Pydantic validation error

    def test_post_review_max_files_capped_at_10(self):
        state = _make_mock_state()
        mock_session = _make_mock_session()
        state.orchestrator.review_repo.return_value = mock_session
        client = _get_test_client(state)
        client.post("/review/", json={
            "repo_url": "https://github.com/test/repo",
            "max_files": 999,
        })
        call_kwargs = state.orchestrator.review_repo.call_args[1]
        assert call_kwargs["max_files"] <= 10

    def test_get_review_by_session_id(self):
        client = self._client_with_mock_review("known-id")
        # First create it
        client.post("/review/", json={"repo_url": "https://github.com/test/repo"})
        # Then fetch it
        r = client.get("/review/known-id")
        assert r.status_code == 200
        assert r.json()["session_id"] == "known-id"

    def test_get_review_404_for_unknown_id(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        r = client.get("/review/does-not-exist-xyz")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_list_reviews_returns_list(self):
        client = self._client_with_mock_review()
        r = client.get("/review/list")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_post_review_500_on_orchestrator_exception(self):
        state = _make_mock_state()
        state.orchestrator.review_repo.side_effect = RuntimeError("DB exploded")
        client = _get_test_client(state)
        r = client.post("/review/", json={"repo_url": "https://github.com/test/repo"})
        assert r.status_code == 500
        assert "Review failed" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════════════
# Memory endpoint tests
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryEndpoints:

    def _client_with_neo4j(self, connected: bool = True) -> TestClient:
        """Return a client with a mock Neo4jClient."""
        state = _make_mock_state(neo4j_connected=connected)
        return _get_test_client(state)

    def _patch_neo4j(self, connected: bool = True, node_counts=None, rel_total: int = 10):
        """Context manager that patches Neo4jClient.get_instance()."""
        mock_client = MagicMock()
        mock_client.is_connected = connected
        if connected:
            mock_client.query.return_value = [
                {"label": k, "count": v}
                for k, v in (node_counts or {"Repo": 1, "File": 3, "Review": 5, "Issue": 4}).items()
            ]
            mock_client.query_single.return_value = {"total": rel_total}
        return patch(
            "src.api.routers.memory.Neo4jClient.get_instance",
            return_value=mock_client,
        )

    def test_memory_stats_200_when_connected(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        with self._patch_neo4j(connected=True):
            r = client.get("/memory/stats")
        assert r.status_code == 200

    def test_memory_stats_connected_true(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        with self._patch_neo4j(connected=True):
            data = client.get("/memory/stats").json()
        assert data["connected"] is True
        assert "node_counts" in data
        assert data["total_relationships"] == 10

    def test_memory_stats_connected_false_graceful(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        with self._patch_neo4j(connected=False):
            r = client.get("/memory/stats")
        assert r.status_code == 200
        assert r.json()["connected"] is False
        assert r.json()["total_relationships"] == 0

    def test_memory_file_404_when_not_found(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.query_single.return_value = None  # file not in graph
        with patch("src.api.routers.memory.Neo4jClient.get_instance", return_value=mock_client):
            r = client.get("/memory/file", params={
                "file_path": "/tmp/unknown.py",
                "repo_url": "https://github.com/test/repo",
            })
        assert r.status_code == 404

    def test_memory_file_200_when_found(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.query_single.return_value = {"review_count": 3, "avg_risk": 0.6}
        mock_client.query.return_value = []
        with patch("src.api.routers.memory.Neo4jClient.get_instance", return_value=mock_client):
            r = client.get("/memory/file", params={
                "file_path": "/tmp/known.py",
                "repo_url": "https://github.com/test/repo",
            })
        assert r.status_code == 200
        data = r.json()
        assert data["review_count"] == 3
        assert abs(data["avg_risk_score"] - 0.6) < 0.01

    def test_memory_file_503_when_disconnected(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        mock_client = MagicMock()
        mock_client.is_connected = False
        with patch("src.api.routers.memory.Neo4jClient.get_instance", return_value=mock_client):
            r = client.get("/memory/file", params={
                "file_path": "/tmp/x.py",
                "repo_url": "https://github.com/test/repo",
            })
        assert r.status_code == 503

    def test_memory_patterns_200(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.query.return_value = [
            {
                "description": "HIGH security issues across 3 files",
                "category": "security",
                "severity": "HIGH",
                "occurrences": 3,
                "affected_files": 3,
            }
        ]
        with patch("src.api.routers.memory.Neo4jClient.get_instance", return_value=mock_client):
            r = client.get("/memory/patterns", params={
                "repo_url": "https://github.com/test/repo"
            })
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 1
        assert len(data["patterns"]) == 1

    def test_memory_delete_repo_200(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.query.return_value = []
        with patch("src.api.routers.memory.Neo4jClient.get_instance", return_value=mock_client):
            r = client.delete("/memory/repo", params={
                "repo_url": "https://github.com/test/repo"
            })
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    def test_memory_delete_repo_503_when_disconnected(self):
        state = _make_mock_state()
        client = _get_test_client(state)
        mock_client = MagicMock()
        mock_client.is_connected = False
        with patch("src.api.routers.memory.Neo4jClient.get_instance", return_value=mock_client):
            r = client.delete("/memory/repo", params={
                "repo_url": "https://github.com/test/repo"
            })
        assert r.status_code == 503


# ══════════════════════════════════════════════════════════════════════════════
# AppState singleton tests
# ══════════════════════════════════════════════════════════════════════════════

class TestAppStateSingleton:

    def test_get_instance_always_returns_same_object(self):
        from src.api.dependencies import AppState
        # Reset singleton for clean test
        original = AppState._instance
        AppState._instance = None
        try:
            a = AppState.get_instance()
            b = AppState.get_instance()
            c = AppState.get_instance()
            assert a is b
            assert b is c
        finally:
            AppState._instance = original

    def test_review_count_increments(self):
        from src.api.dependencies import AppState
        original = AppState._instance
        AppState._instance = None
        try:
            s = AppState.get_instance()
            assert s.review_count == 0
            s.increment_review_count()
            s.increment_review_count()
            assert s.review_count == 2
        finally:
            AppState._instance = original

    def test_uptime_increases_over_time(self):
        from src.api.dependencies import AppState
        original = AppState._instance
        AppState._instance = None
        try:
            s = AppState.get_instance()
            t1 = s.uptime_seconds
            time.sleep(0.05)
            t2 = s.uptime_seconds
            assert t2 > t1
        finally:
            AppState._instance = original


# ══════════════════════════════════════════════════════════════════════════════
# Pydantic model validation tests
# ══════════════════════════════════════════════════════════════════════════════

class TestReviewRequestModel:

    def test_valid_request(self):
        from src.api.models import ReviewRequest
        r = ReviewRequest(repo_url="https://github.com/test/repo")
        assert r.repo_url == "https://github.com/test/repo"
        assert r.max_files == 5

    def test_max_files_capped_at_10(self):
        from src.api.models import ReviewRequest
        r = ReviewRequest(repo_url="https://github.com/test/repo", max_files=999)
        assert r.max_files == 10

    def test_max_files_5_stays_5(self):
        from src.api.models import ReviewRequest
        r = ReviewRequest(repo_url="https://github.com/test/repo", max_files=5)
        assert r.max_files == 5

    def test_invalid_url_raises(self):
        from src.api.models import ReviewRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ReviewRequest(repo_url="not-a-url")

    def test_trailing_slash_stripped(self):
        from src.api.models import ReviewRequest
        r = ReviewRequest(repo_url="https://github.com/test/repo/")
        assert not r.repo_url.endswith("/")

    def test_defaults(self):
        from src.api.models import ReviewRequest
        r = ReviewRequest(repo_url="https://github.com/test/repo")
        assert r.use_memory is True
        assert r.use_defect_api is False
        assert r.files is None