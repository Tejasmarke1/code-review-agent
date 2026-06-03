"""
Unit tests for the ReAct loop — no LLM calls needed.
Uses mock LLM responses to test parsing, state management, and tool dispatch.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.react_loop import ReActLoop
from src.agent.state import AgentState, AgentStatus, ReviewIssue, Severity
from src.tools.registry import Tool, ToolRegistry


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_groq():
    """Return a mock GroqClient that records calls."""
    client = MagicMock()
    client.model = "llama-3.3-70b-versatile"
    client.complete.return_value = (
        'Thought: I should read the file first.\n'
        'Action: read_file\n'
        'Action Input: {"file_path": "/tmp/test.py"}'
    )
    return client


@pytest.fixture
def registry():
    """Return a fresh ToolRegistry with a simple echo tool."""
    reg = ToolRegistry()

    def echo(message: str) -> str:
        return f"echo: {message}"

    reg.register(Tool(
        name="echo",
        description="Echo a message back.",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo"},
            },
            "required": ["message"],
        },
        func=echo,
        category="test",
    ))
    return reg


@pytest.fixture
def mock_prompt_engine():
    """Return a mock PromptEngine."""
    engine = MagicMock()
    engine.build_system_prompt.return_value = "You are a code reviewer."
    engine.build_initial_prompt.return_value = [
        {"role": "system", "content": "You are a code reviewer."},
        {"role": "user",   "content": "Review this file. Thought:"},
    ]
    engine.build_continuation_prompt.return_value = [
        {"role": "system", "content": "You are a code reviewer."},
    ]
    engine.build_reflection_prompt.return_value = [
        {"role": "system", "content": "You are a code reviewer."},
        {"role": "user",   "content": "Reflect. Thought:"},
    ]
    engine.build_parse_error_recovery_prompt.return_value = [
        {"role": "system", "content": "You are a code reviewer."},
        {"role": "user",   "content": "Fix your format. Thought:"},
    ]
    return engine


@pytest.fixture
def react_loop(mock_groq, registry, mock_prompt_engine):
    """Return a ReActLoop wired to mock collaborators."""
    return ReActLoop(
        groq_client=mock_groq,
        tool_registry=registry,
        prompt_engine=mock_prompt_engine,
    )


def make_state(**kwargs) -> AgentState:
    """Create a minimal AgentState for testing."""
    defaults = dict(
        session_id="test01",
        repo_url="https://github.com/test/repo",
        file_path="/tmp/test.py",
        file_content="x = 1\n",
    )
    defaults.update(kwargs)
    return AgentState(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# Tests: _parse_response
# ══════════════════════════════════════════════════════════════════════════════

class TestParseResponse:
    """Unit tests for the LLM response parser."""

    def _get_loop(self, registry=None):
        """Return a bare ReActLoop with minimal mocks."""
        reg = registry or ToolRegistry()
        return ReActLoop(
            groq_client=MagicMock(),
            tool_registry=reg,
            prompt_engine=MagicMock(),
        )

    def test_valid_response_returns_tuple(self):
        """Well-formatted response should return (thought, action, input)."""
        loop = self._get_loop()
        state = make_state()
        response = (
            'Thought: I need to read the file to understand its content.\n'
            'Action: read_file\n'
            'Action Input: {"file_path": "/tmp/example.py"}'
        )
        result = loop._parse_response(response, state)
        assert result is not None
        thought, action, action_input = result
        assert "read the file" in thought
        assert action == "read_file"
        assert action_input == {"file_path": "/tmp/example.py"}

    def test_missing_action_returns_none(self):
        """Response with no Action: field should return None and increment failure count."""
        loop = self._get_loop()
        state = make_state()
        response = 'Thought: Something.\nAction Input: {"file_path": "/tmp/x.py"}'
        result = loop._parse_response(response, state)
        assert result is None
        assert loop._consecutive_parse_failures == 1

    def test_malformed_json_returns_none(self):
        """Unfixable JSON in Action Input should return None."""
        loop = self._get_loop()
        state = make_state()
        response = (
            'Thought: Check it.\n'
            'Action: run_ruff\n'
            'Action Input: {file_path: /tmp/x.py, broken: }'
        )
        result = loop._parse_response(response, state)
        assert result is None
        assert loop._consecutive_parse_failures >= 1

    def test_single_quoted_json_is_fixed(self):
        """Single-quoted JSON keys/values should be auto-corrected to double quotes."""
        loop = self._get_loop()
        state = make_state()
        response = (
            "Thought: Inspect the file.\n"
            "Action: read_file\n"
            "Action Input: {'file_path': '/tmp/test.py'}"
        )
        result = loop._parse_response(response, state)
        assert result is not None
        _, action, action_input = result
        assert action == "read_file"
        assert action_input.get("file_path") == "/tmp/test.py"

    def test_finish_review_detected(self):
        """finish_review action should be parsed correctly."""
        loop = self._get_loop()
        state = make_state()
        response = (
            'Thought: I have enough data to finish.\n'
            'Action: finish_review\n'
            'Action Input: {"summary": "Found 3 issues.", "detailed_review": "## Review\\n..."}'
        )
        result = loop._parse_response(response, state)
        assert result is not None
        _, action, action_input = result
        assert action == "finish_review"
        assert "summary" in action_input
        assert "detailed_review" in action_input

    def test_bare_file_path_action_input(self):
        """A bare .py path as Action Input should be wrapped in a file_path key."""
        loop = self._get_loop()
        state = make_state()
        response = (
            "Thought: Read the file.\n"
            "Action: read_file\n"
            "Action Input: /tmp/example.py"
        )
        result = loop._parse_response(response, state)
        assert result is not None
        _, action, action_input = result
        assert action_input.get("file_path") == "/tmp/example.py"

    def test_trailing_comma_json_is_fixed(self):
        """JSON with a trailing comma before closing brace should be auto-corrected."""
        loop = self._get_loop()
        state = make_state()
        response = (
            'Thought: Run linter.\n'
            'Action: run_ruff\n'
            'Action Input: {"file_path": "/tmp/x.py",}'
        )
        result = loop._parse_response(response, state)
        assert result is not None
        _, _, action_input = result
        assert action_input.get("file_path") == "/tmp/x.py"

    def test_parse_failure_counter_increments(self):
        """Each parse failure should increment _consecutive_parse_failures."""
        loop = self._get_loop()
        state = make_state()
        bad = "This is not formatted at all."
        loop._parse_response(bad, state)
        loop._parse_response(bad, state)
        assert loop._consecutive_parse_failures == 2

    def test_successful_parse_does_not_increment_failure_count(self):
        """A successful parse should not change the failure counter."""
        loop = self._get_loop()
        state = make_state()
        loop._consecutive_parse_failures = 2  # simulate prior failures
        good = (
            'Thought: Check security.\n'
            'Action: run_bandit\n'
            'Action Input: {"file_path": "/tmp/x.py"}'
        )
        result = loop._parse_response(good, state)
        assert result is not None
        # Counter is reset by caller (_consecutive_parse_failures = 0) only in run()
        # The parse itself should not increment it further
        assert loop._consecutive_parse_failures == 2


# ══════════════════════════════════════════════════════════════════════════════
# Tests: ToolRegistry
# ══════════════════════════════════════════════════════════════════════════════

class TestToolRegistry:
    """Unit tests for ToolRegistry.call() error handling."""

    def test_unknown_tool_returns_error_string(self):
        """Calling an unregistered tool name should return an error string."""
        reg = ToolRegistry()
        result = reg.call("nonexistent_tool", {})
        assert "ERROR" in result
        assert "nonexistent_tool" in result
        assert "Available tools" in result

    def test_unknown_tool_does_not_raise(self):
        """Calling an unknown tool must never raise an exception."""
        reg = ToolRegistry()
        try:
            result = reg.call("ghost", {"x": 1})
        except Exception as e:
            pytest.fail(f"ToolRegistry.call() raised an exception: {e}")

    def test_missing_required_param_returns_error(self):
        """Calling a tool without its required parameter returns a helpful error."""
        reg = ToolRegistry()

        def greet(name: str) -> str:
            return f"Hello, {name}"

        reg.register(Tool(
            name="greet",
            description="Greet someone.",
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Name"}},
                "required": ["name"],
            },
            func=greet,
            category="test",
        ))
        result = reg.call("greet", {})
        assert "ERROR" in result
        assert "name" in result

    def test_tool_exception_returns_error_string(self):
        """A tool that raises an exception should return an error string, not propagate."""
        reg = ToolRegistry()

        def exploder() -> str:
            raise ValueError("Something went wrong internally")

        reg.register(Tool(
            name="exploder",
            description="Always explodes.",
            parameters={"type": "object", "properties": {}, "required": []},
            func=exploder,
            category="test",
        ))
        result = reg.call("exploder", {})
        assert "ERROR" in result
        assert "exploder" in result
        assert "ValueError" in result

    def test_successful_tool_call_returns_string(self):
        """A successful tool call should return its result as a string."""
        reg = ToolRegistry()

        def add(a: int, b: int) -> int:
            return a + b

        reg.register(Tool(
            name="add",
            description="Add two numbers.",
            parameters={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
            func=add,
            category="test",
        ))
        result = reg.call("add", {"a": 3, "b": 4})
        assert result == "7"

    def test_duplicate_registration_raises(self):
        """Registering two tools with the same name should raise ValueError."""
        reg = ToolRegistry()
        tool = Tool(
            name="dupe",
            description=".",
            parameters={"type": "object", "properties": {}, "required": []},
            func=lambda: None,
            category="test",
        )
        reg.register(tool)
        with pytest.raises(ValueError, match="already registered"):
            reg.register(tool)

    def test_call_counts_are_tracked(self):
        """ToolRegistry should track how many times each tool is called."""
        reg = ToolRegistry()

        def noop() -> str:
            return "ok"

        reg.register(Tool(
            name="noop",
            description=".",
            parameters={"type": "object", "properties": {}, "required": []},
            func=noop,
            category="test",
        ))
        reg.call("noop", {})
        reg.call("noop", {})
        reg.call("noop", {})
        assert reg.get_call_stats()["noop"] == 3

    def test_contains_operator(self):
        """The `in` operator should work for tool name membership checks."""
        reg = ToolRegistry()
        reg.register(Tool(
            name="ping",
            description=".",
            parameters={"type": "object", "properties": {}, "required": []},
            func=lambda: "pong",
            category="test",
        ))
        assert "ping" in reg
        assert "pong" not in reg


# ══════════════════════════════════════════════════════════════════════════════
# Tests: AgentState
# ══════════════════════════════════════════════════════════════════════════════

class TestAgentState:
    """Unit tests for AgentState data management."""

    def test_add_step_increments_current_step(self):
        """add_step() should increment current_step by 1 each call."""
        state = make_state()
        assert state.current_step == 0
        state.add_step("think", "act", {"k": "v"}, "obs")
        assert state.current_step == 1
        state.add_step("think2", "act2", {}, "obs2")
        assert state.current_step == 2

    def test_add_step_records_thought_history(self):
        """add_step() should append to thought_history."""
        state = make_state()
        state.add_step("my thought", "run_ruff", {"file_path": "x.py"}, "found 3 issues")
        assert len(state.thought_history) == 1
        step = state.thought_history[0]
        assert step.thought == "my thought"
        assert step.action == "run_ruff"
        assert step.action_input == {"file_path": "x.py"}
        assert step.observation == "found 3 issues"

    def test_tools_called_tracks_actions(self):
        """tools_called should accumulate action names in order."""
        state = make_state()
        state.add_step("t1", "read_file", {}, "obs")
        state.add_step("t2", "run_ruff",  {}, "obs")
        state.add_step("t3", "run_ruff",  {}, "obs")
        assert state.tools_called == ["read_file", "run_ruff", "run_ruff"]

    def test_issue_count_by_severity_is_correct(self):
        """issue_count_by_severity should count correctly across all severities."""
        state = make_state()
        state.add_issue(ReviewIssue(
            file_path="x.py", line_number=1,
            severity=Severity.HIGH, category="security",
            title="Bad", description=".", suggestion="Fix",
            source_tool="bandit", confidence=0.9,
        ))
        state.add_issue(ReviewIssue(
            file_path="x.py", line_number=2,
            severity=Severity.LOW, category="style",
            title="Minor", description=".", suggestion="Fix",
            source_tool="ruff", confidence=0.7,
        ))
        state.add_issue(ReviewIssue(
            file_path="x.py", line_number=3,
            severity=Severity.HIGH, category="logic",
            title="Logic bug", description=".", suggestion="Fix",
            source_tool="agent_reasoning", confidence=0.8,
        ))
        counts = state.issue_count_by_severity
        assert counts["HIGH"] == 2
        assert counts["LOW"] == 1
        assert counts["MEDIUM"] == 0
        assert counts["CRITICAL"] == 0

    def test_highest_severity_returns_correct_enum(self):
        """highest_severity should return the maximum Severity across all issues."""
        state = make_state()
        for sev in [Severity.LOW, Severity.MEDIUM, Severity.CRITICAL, Severity.HIGH]:
            state.add_issue(ReviewIssue(
                file_path="x.py", line_number=None,
                severity=sev, category="test",
                title="T", description=".", suggestion=".",
                source_tool="test", confidence=1.0,
            ))
        assert state.highest_severity == Severity.CRITICAL

    def test_highest_severity_none_when_no_issues(self):
        """highest_severity should return None when no issues have been recorded."""
        state = make_state()
        assert state.highest_severity is None

    def test_elapsed_seconds_is_positive(self):
        """elapsed_seconds should be a positive float."""
        state = make_state()
        time.sleep(0.01)   # ensure at least 10 ms passes
        assert state.elapsed_seconds > 0.0

    def test_elapsed_seconds_uses_completed_at_when_set(self):
        """elapsed_seconds should use completed_at instead of now() when set."""
        from datetime import datetime, timedelta
        state = make_state()
        state.completed_at = state.started_at + timedelta(seconds=5)
        assert 4.9 < state.elapsed_seconds < 5.1

    def test_initial_status_is_running(self):
        """Freshly created AgentState should have RUNNING status."""
        state = make_state()
        assert state.status == AgentStatus.RUNNING


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Severity ordering
# ══════════════════════════════════════════════════════════════════════════════

class TestSeverityOrdering:
    """Ensure Severity comparison operators work correctly."""

    def test_critical_greater_than_high(self):
        assert Severity.CRITICAL > Severity.HIGH

    def test_high_greater_than_medium(self):
        assert Severity.HIGH > Severity.MEDIUM

    def test_medium_greater_than_low(self):
        assert Severity.MEDIUM > Severity.LOW

    def test_equal_severities(self):
        assert not (Severity.HIGH > Severity.HIGH)
        assert Severity.HIGH >= Severity.HIGH

    def test_max_of_severities(self):
        severities = [Severity.LOW, Severity.CRITICAL, Severity.MEDIUM]
        assert max(severities) == Severity.CRITICAL