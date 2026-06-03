"""
Agent state dataclass.
Immutable snapshot of agent progress at each ReAct step.
Passed into every tool call and LLM prompt.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class AgentStatus(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    MAX_STEPS_REACHED = "max_steps_reached"


class Severity(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    def __gt__(self, other: "Severity") -> bool:
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(self) > order.index(other)

    def __ge__(self, other: "Severity") -> bool:
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(self) >= order.index(other)

    def __lt__(self, other: "Severity") -> bool:
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(self) < order.index(other)

    def __le__(self, other: "Severity") -> bool:
        order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        return order.index(self) <= order.index(other)


@dataclass
class ThoughtStep:
    """One complete Think→Act→Observe cycle."""
    step_number: int
    thought: str                    # agent's reasoning
    action: str                     # tool name chosen
    action_input: dict[str, Any]    # tool arguments
    observation: str                # tool output
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ReviewIssue:
    """A single issue found during review."""
    file_path: str
    line_number: Optional[int]
    severity: Severity
    category: str                   # "security" / "style" / "complexity" / "logic" / "performance"
    title: str                      # short title, max 80 chars
    description: str                # detailed explanation
    suggestion: str                 # how to fix it
    source_tool: str                # "ruff" / "bandit" / "agent_reasoning" / "radon"
    confidence: float               # 0.0 to 1.0


@dataclass
class AgentState:
    """
    Complete agent state at any point during execution.
    One AgentState per file being reviewed.
    """
    # Identity
    session_id: str
    repo_url: str
    file_path: str
    file_content: str

    # Risk context from Defect Prediction Engine
    risk_score: float = 0.0
    risk_label: str = "UNKNOWN"     # HIGH / MEDIUM / LOW
    shap_features: list[dict] = field(default_factory=list)  # top SHAP features from ML model

    # Execution state
    status: AgentStatus = AgentStatus.RUNNING
    current_step: int = 0
    thought_history: list[ThoughtStep] = field(default_factory=list)

    # Findings
    issues_found: list[ReviewIssue] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)

    # Memory context (populated Day 3)
    past_issues_context: str = ""   # relevant past issues retrieved from Neo4j
    recurring_patterns: list[str] = field(default_factory=list)

    # Timing
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    # Final output
    final_review: str = ""          # markdown-formatted review report
    summary: str = ""               # one-paragraph summary

    def add_step(self, thought: str, action: str, action_input: dict, observation: str) -> None:
        """Record one completed Thought→Action→Observation cycle and advance the step counter.

        Args:
            thought: The agent's reasoning text for this step.
            action: The tool name that was called.
            action_input: The arguments passed to the tool.
            observation: The string result returned by the tool.
        """
        self.thought_history.append(ThoughtStep(
            step_number=self.current_step,
            thought=thought,
            action=action,
            action_input=action_input,
            observation=observation,
        ))
        self.tools_called.append(action)
        self.current_step += 1

    def add_issue(self, issue: ReviewIssue) -> None:
        """Append a discovered ReviewIssue to the findings list.

        Args:
            issue: The ReviewIssue instance to record.
        """
        self.issues_found.append(issue)

    @property
    def issue_count_by_severity(self) -> dict[str, int]:
        """Return a dict mapping each Severity level name to the count of issues at that level."""
        counts = {s.value: 0 for s in Severity}
        for issue in self.issues_found:
            counts[issue.severity.value] += 1
        return counts

    @property
    def highest_severity(self) -> Optional[Severity]:
        """Return the highest Severity found across all issues, or None if no issues exist."""
        if not self.issues_found:
            return None
        return max(i.severity for i in self.issues_found)

    @property
    def elapsed_seconds(self) -> float:
        """Return the number of seconds elapsed since the review started."""
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()