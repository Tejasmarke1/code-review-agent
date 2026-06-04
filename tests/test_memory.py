"""
Unit tests for memory layer — uses a mock Neo4j client.

Tests:
- MemoryWriter.write_review() with completed AgentState → correct nodes written
- MemoryWriter deduplication: same issue twice → occurrence_count incremented
- MemoryWriter._detect_and_promote_patterns() → pattern created at threshold=3
- MemoryRetriever.get_file_context() → returns formatted string
- MemoryRetriever.get_file_context() with empty graph → returns empty string
- Neo4jClient.query() when not connected → raises RuntimeError with clear message
- MemoryTools functions return strings not exceptions when Neo4j unavailable

All tests use unittest.mock — no real DB connection needed.
Run with: pytest tests/test_memory.py -v
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.state import AgentState, AgentStatus, ReviewIssue, Severity
from src.memory.memory_writer import MemoryWriter
from src.memory.memory_retriever import MemoryRetriever
from src.memory.neo4j_client import Neo4jClient


# ── Fixtures ───────────────────────────────────────────────────────────────────

def make_agent_state(
    file_path: str = "/tmp/test.py",
    repo_url: str = "https://github.com/test/repo",
    issues: list[ReviewIssue] | None = None,
) -> AgentState:
    """Create a completed AgentState suitable for memory tests."""
    state = AgentState(
        session_id=str(uuid.uuid4())[:8],
        repo_url=repo_url,
        file_path=file_path,
        file_content="x = 1\npassword = 'secret'\n",
    )
    state.status = AgentStatus.COMPLETED
    state.current_step = 3
    state.tools_called = ["read_file", "run_bandit", "finish_review"]
    state.risk_score = 0.75
    state.risk_label = "HIGH"
    if issues:
        for issue in issues:
            state.add_issue(issue)
    return state


def make_issue(
    title: str = "Hardcoded password",
    severity: Severity = Severity.HIGH,
    category: str = "security",
    source_tool: str = "bandit",
    file_path: str = "/tmp/test.py",
) -> ReviewIssue:
    return ReviewIssue(
        file_path=file_path,
        line_number=2,
        severity=severity,
        category=category,
        title=title,
        description="A password is hardcoded in the source.",
        suggestion="Use environment variables.",
        source_tool=source_tool,
        confidence=0.95,
    )


@pytest.fixture
def mock_client() -> MagicMock:
    """Return a mock Neo4jClient that behaves as connected."""
    client = MagicMock(spec=Neo4jClient)
    client.is_connected = True
    client.query.return_value = []
    client.query_single.return_value = None
    return client


@pytest.fixture
def writer(mock_client: MagicMock) -> MemoryWriter:
    return MemoryWriter(mock_client)


@pytest.fixture
def retriever(mock_client: MagicMock) -> MemoryRetriever:
    return MemoryRetriever(mock_client)


# ══════════════════════════════════════════════════════════════════════════════
# Tests: MemoryWriter.write_review()
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryWriterWriteReview:
    """Tests for the top-level write_review() method."""

    def test_write_review_calls_all_private_helpers(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """write_review() should call upsert_repo, upsert_file, create_review, etc."""
        state = make_agent_state(issues=[make_issue()])
        result = writer.write_review(state)
        # At minimum, query() must have been called (repo + file + review + issue)
        assert mock_client.query.call_count >= 4
        assert "issues_written" in result
        assert "patterns_detected" in result

    def test_write_review_returns_correct_counts(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """Return dict must include nodes_created and issues_written."""
        state = make_agent_state(issues=[make_issue(), make_issue("Second issue")])
        result = writer.write_review(state)
        assert result["issues_written"] == 2
        assert result["nodes_created"] >= 2

    def test_write_review_without_connection_returns_zero_dict(self) -> None:
        """write_review() must not raise when Neo4j is disconnected."""
        disconnected = MagicMock(spec=Neo4jClient)
        disconnected.is_connected = False
        w = MemoryWriter(disconnected)
        state = make_agent_state()
        result = w.write_review(state)
        assert result == {
            "nodes_created": 0,
            "issues_written": 0,
            "patterns_detected": 0,
        }

    def test_write_review_never_raises_on_query_failure(
        self, mock_client: MagicMock
    ) -> None:
        """If any Cypher call fails, write_review() must catch it and return error key."""
        mock_client.query.side_effect = RuntimeError("DB exploded")
        w = MemoryWriter(mock_client)
        state = make_agent_state()
        result = w.write_review(state)
        assert "error" in result
        assert result["nodes_created"] == 0

    def test_write_review_with_no_issues(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """Review with zero issues should still write repo/file/review nodes."""
        state = make_agent_state(issues=[])
        result = writer.write_review(state)
        assert result["issues_written"] == 0
        # Repo, file, and review queries must still have run
        assert mock_client.query.call_count >= 3


# ══════════════════════════════════════════════════════════════════════════════
# Tests: MemoryWriter deduplication
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryWriterDeduplication:
    """Tests for issue deduplication logic inside _write_issues()."""

    def test_existing_issue_increments_count_not_duplicated(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """When the same issue already exists, occurrence_count must be incremented."""
        existing_id = str(uuid.uuid4())
        # Simulate existing match on dedup lookup
        mock_client.query_single.return_value = {"issue_id": existing_id}

        state = make_agent_state(issues=[make_issue()])
        writer._write_issues(state, state.session_id)

        # The UPDATE query (SET occurrence_count) must have been called
        update_calls = [
            str(c) for c in mock_client.query.call_args_list
            if "occurrence_count" in str(c) and "+" in str(c)
        ]
        assert len(update_calls) >= 1, "Expected occurrence_count increment query"

    def test_new_issue_creates_node(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """When no existing issue matches, a new Issue node must be created."""
        mock_client.query_single.return_value = None  # no existing match

        state = make_agent_state(issues=[make_issue()])
        written = writer._write_issues(state, state.session_id)

        # CREATE query must have been called
        create_calls = [
            c for c in mock_client.query.call_args_list
            if "CREATE (i:Issue" in str(c)
        ]
        assert len(create_calls) >= 1
        assert written == 1

    def test_two_identical_issues_one_new_one_update(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """Two issues with identical dedup key: first creates, second increments."""
        # First call: no existing → create
        # Second call: existing found → update
        existing_id = str(uuid.uuid4())
        mock_client.query_single.side_effect = [None, {"issue_id": existing_id}]

        issue = make_issue()
        state = make_agent_state(issues=[issue, issue])
        written = writer._write_issues(state, state.session_id)

        assert written == 2  # both processed


# ══════════════════════════════════════════════════════════════════════════════
# Tests: MemoryWriter pattern detection
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryWriterPatternDetection:
    """Tests for _detect_and_promote_patterns()."""

    def test_pattern_created_when_threshold_met(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """A new Pattern node must be created when category+severity spans 3+ files."""
        mock_client.query.return_value = [
            {
                "category": "security",
                "severity": "HIGH",
                "files": ["/a.py", "/b.py", "/c.py"],
                "file_count": 3,
            }
        ]
        mock_client.query_single.return_value = None  # no existing pattern

        new_patterns = writer._detect_and_promote_patterns(
            "https://github.com/test/repo"
        )

        create_calls = [
            c for c in mock_client.query.call_args_list
            if "CREATE (p:Pattern" in str(c)
        ]
        assert len(create_calls) >= 1
        assert new_patterns == 1

    def test_existing_pattern_is_updated_not_duplicated(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """When a matching Pattern already exists, update it — don't create a duplicate."""
        mock_client.query.return_value = [
            {
                "category": "security",
                "severity": "HIGH",
                "files": ["/a.py", "/b.py", "/c.py"],
                "file_count": 3,
            }
        ]
        mock_client.query_single.return_value = {
            "pattern_id": "https://github.com/test/repo:security:HIGH"
        }

        new_patterns = writer._detect_and_promote_patterns(
            "https://github.com/test/repo"
        )

        create_calls = [
            c for c in mock_client.query.call_args_list
            if "CREATE (p:Pattern" in str(c)
        ]
        assert len(create_calls) == 0  # no new node
        assert new_patterns == 0

    def test_no_pattern_below_threshold(
        self, writer: MemoryWriter, mock_client: MagicMock
    ) -> None:
        """No patterns should be created when no candidate meets the threshold."""
        mock_client.query.return_value = []  # no candidates

        new_patterns = writer._detect_and_promote_patterns(
            "https://github.com/test/repo"
        )
        assert new_patterns == 0


# ══════════════════════════════════════════════════════════════════════════════
# Tests: MemoryRetriever
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryRetriever:
    """Tests for MemoryRetriever.get_file_context() and sub-queries."""

    def test_get_file_context_returns_formatted_string(
        self, retriever: MemoryRetriever, mock_client: MagicMock
    ) -> None:
        """get_file_context() must return a non-empty string when past data exists."""
        mock_client.query.return_value = [
            {
                "title": "Hardcoded password",
                "severity": "HIGH",
                "category": "security",
                "tool": "bandit",
                "line": 2,
                "occurrences": 3,
                "last_seen": "2025-01-01T00:00:00",
            }
        ]
        mock_client.query_single.return_value = {
            "total_reviews": 5,
            "files_reviewed": 3,
            "total_issues": 12,
            "avg_risk": 0.72,
        }

        ctx = retriever.get_file_context("/tmp/test.py", "https://github.com/test/repo")
        assert ctx != ""
        assert "MEMORY CONTEXT" in ctx

    def test_get_file_context_returns_empty_when_no_history(
        self, retriever: MemoryRetriever, mock_client: MagicMock
    ) -> None:
        """get_file_context() must return empty string when graph has no data."""
        mock_client.query.return_value = []
        mock_client.query_single.return_value = None

        ctx = retriever.get_file_context("/tmp/new.py", "https://github.com/test/repo")
        assert ctx == ""

    def test_get_file_context_returns_empty_when_disconnected(self) -> None:
        """get_file_context() must return empty string when Neo4j is not connected."""
        disconnected = MagicMock(spec=Neo4jClient)
        disconnected.is_connected = False
        r = MemoryRetriever(disconnected)
        ctx = r.get_file_context("/tmp/test.py", "https://github.com/test/repo")
        assert ctx == ""

    def test_get_file_context_never_raises_on_query_error(
        self, mock_client: MagicMock
    ) -> None:
        """Even if Cypher queries raise, get_file_context() must return empty string."""
        mock_client.query.side_effect = Exception("Connection dropped")
        r = MemoryRetriever(mock_client)
        ctx = r.get_file_context("/tmp/test.py", "https://github.com/test/repo")
        assert ctx == ""  # graceful fallback

    def test_past_issues_section_shows_recurrence(
        self, retriever: MemoryRetriever, mock_client: MagicMock
    ) -> None:
        """Issues with occurrence_count > 1 should show '(seen Nx)' in context."""
        mock_client.query.return_value = [
            {
                "title": "SQL Injection",
                "severity": "CRITICAL",
                "category": "security",
                "tool": "bandit",
                "line": 10,
                "occurrences": 4,
                "last_seen": "2025-06-01T00:00:00",
            }
        ]
        mock_client.query_single.return_value = None

        result = retriever._get_past_issues_for_file(
            "/tmp/test.py", "https://github.com/test/repo"
        )
        assert "4x" in result or "seen 4" in result

    def test_active_patterns_section_formatted_correctly(
        self, retriever: MemoryRetriever, mock_client: MagicMock
    ) -> None:
        """Active patterns must appear in the context with severity labels."""
        mock_client.query.return_value = [
            {
                "description": "HIGH security issues across 4 files",
                "category": "security",
                "severity": "HIGH",
                "affected_count": 4,
            }
        ]
        result = retriever._get_active_patterns("https://github.com/test/repo")
        assert "[HIGH]" in result
        assert "4 files" in result

    def test_repo_stats_empty_for_new_repo(
        self, retriever: MemoryRetriever, mock_client: MagicMock
    ) -> None:
        """_get_repo_stats() must return empty string for a brand-new repo."""
        mock_client.query_single.return_value = None
        result = retriever._get_repo_stats("https://github.com/new/repo")
        assert result == ""

    def test_get_similar_files_returns_list(
        self, retriever: MemoryRetriever, mock_client: MagicMock
    ) -> None:
        """get_similar_files() must return a list of file path strings."""
        mock_client.query.return_value = [
            {"similar_file": "/tmp/other_a.py"},
            {"similar_file": "/tmp/other_b.py"},
        ]
        result = retriever.get_similar_files(
            "/tmp/test.py", "https://github.com/test/repo", limit=2
        )
        assert isinstance(result, list)
        assert len(result) == 2
        assert "/tmp/other_a.py" in result


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Neo4jClient
# ══════════════════════════════════════════════════════════════════════════════

class TestNeo4jClient:
    """Tests for Neo4jClient core behaviour (no real DB)."""

    def test_query_raises_when_not_connected(self) -> None:
        """query() must raise RuntimeError with a clear message if not connected."""
        client = Neo4jClient()
        # Do not call connect() — so _connected stays False
        with pytest.raises(RuntimeError, match="not connected"):
            client.query("MATCH (n) RETURN n", {})

    def test_query_single_returns_none_for_empty_result(self) -> None:
        """query_single() must return None (not raise) when query returns no rows."""
        client = Neo4jClient()
        client._connected = True
        # Patch the underlying query to return empty list
        with patch.object(client, "query", return_value=[]):
            result = client.query_single("MATCH (n) RETURN n LIMIT 1", {})
            assert result is None

    def test_query_single_returns_first_row(self) -> None:
        """query_single() must return the first row dict from a multi-row result."""
        client = Neo4jClient()
        client._connected = True
        rows = [{"n": "first"}, {"n": "second"}]
        with patch.object(client, "query", return_value=rows):
            result = client.query_single("MATCH (n) RETURN n", {})
            assert result == {"n": "first"}

    def test_singleton_pattern(self) -> None:
        """get_instance() called multiple times must return the same object."""
        # Reset singleton for clean test
        original = Neo4jClient._instance
        Neo4jClient._instance = None

        a = Neo4jClient.get_instance()
        b = Neo4jClient.get_instance()
        c = Neo4jClient.get_instance()
        assert a is b
        assert b is c

        # Restore
        Neo4jClient._instance = original

    def test_is_connected_false_by_default(self) -> None:
        """A fresh Neo4jClient must report is_connected=False before connect()."""
        client = Neo4jClient()
        assert client.is_connected is False


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Memory tool functions (no DB)
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryToolFunctions:
    """Memory tool functions must return strings even when Neo4j is unavailable."""

    def _patch_disconnected_client(self):
        """Context manager that patches Neo4jClient.get_instance to return disconnected mock."""
        mock = MagicMock(spec=Neo4jClient)
        mock.is_connected = False
        return patch(
            "src.tools.memory_tools.Neo4jClient.get_instance",
            return_value=mock,
        )

    def test_search_past_issues_returns_string_when_disconnected(self) -> None:
        from src.tools.memory_tools import search_past_issues
        with self._patch_disconnected_client():
            result = search_past_issues("https://github.com/test/repo")
        assert isinstance(result, str)
        assert "not connected" in result.lower() or "not available" in result.lower()

    def test_get_file_review_history_returns_string_when_disconnected(self) -> None:
        from src.tools.memory_tools import get_file_review_history
        with self._patch_disconnected_client():
            result = get_file_review_history("/tmp/f.py", "https://github.com/test/repo")
        assert isinstance(result, str)
        assert "not" in result.lower()

    def test_get_repo_patterns_returns_string_when_disconnected(self) -> None:
        from src.tools.memory_tools import get_repo_patterns
        with self._patch_disconnected_client():
            result = get_repo_patterns("https://github.com/test/repo")
        assert isinstance(result, str)
        assert "not" in result.lower()

    def test_search_past_issues_filters_by_category(self) -> None:
        """search_past_issues() with category filter must include it in the Cypher params."""
        from src.tools.memory_tools import search_past_issues
        mock = MagicMock(spec=Neo4jClient)
        mock.is_connected = True
        mock.query.return_value = []
        with patch("src.tools.memory_tools.Neo4jClient.get_instance", return_value=mock):
            search_past_issues(
                "https://github.com/test/repo",
                category="security",
                severity="HIGH",
            )
        # query() should have been called with params containing category and severity
        call_kwargs = mock.query.call_args[0][1]  # positional params dict
        assert "category" in call_kwargs
        assert "severity" in call_kwargs

    def test_register_memory_tools_skips_when_disconnected(self) -> None:
        """register_memory_tools() must not raise and must not register when disconnected."""
        from src.tools.memory_tools import register_memory_tools
        from src.tools.registry import ToolRegistry
        reg = ToolRegistry()
        mock = MagicMock(spec=Neo4jClient)
        mock.is_connected = False
        with patch("src.tools.memory_tools.Neo4jClient.get_instance", return_value=mock):
            register_memory_tools(reg)
        # No memory tools should be registered
        assert "search_past_issues" not in reg.list_tool_names()
        assert "get_file_review_history" not in reg.list_tool_names()
        assert "get_repo_patterns" not in reg.list_tool_names()

    def test_register_memory_tools_registers_when_connected(self) -> None:
        """register_memory_tools() must register 3 tools when Neo4j is connected."""
        from src.tools.memory_tools import register_memory_tools
        from src.tools.registry import ToolRegistry
        reg = ToolRegistry()
        mock = MagicMock(spec=Neo4jClient)
        mock.is_connected = True
        with patch("src.tools.memory_tools.Neo4jClient.get_instance", return_value=mock):
            register_memory_tools(reg)
        assert "search_past_issues" in reg.list_tool_names()
        assert "get_file_review_history" in reg.list_tool_names()
        assert "get_repo_patterns" in reg.list_tool_names()