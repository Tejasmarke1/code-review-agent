"""
API Pydantic Models
====================
All request/response schemas for the Code Review Agent REST API.
Uses Pydantic v2 throughout.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, field_validator


# ── Request Models ─────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    """Request body for POST /review/."""

    repo_url: str
    """GitHub or other VCS URL of the repository to review."""

    files: Optional[list[str]] = None
    """Explicit list of file paths to review, or None for auto-detection."""

    max_files: int = 5
    """Maximum number of files to review in one session (hard-capped at 10)."""

    use_memory: bool = True
    """Whether to use Neo4j graph memory for context retrieval and persistence."""

    use_defect_api: bool = False
    """Whether to call the Defect Prediction Engine (defaults False — may not be running)."""

    @field_validator("max_files")
    @classmethod
    def cap_max_files(cls, v: int) -> int:
        """Hard-cap max_files at 10 regardless of what the client sends."""
        return min(v, 10)

    @field_validator("repo_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Ensure repo_url starts with http:// or https://."""
        if not v.startswith("http"):
            raise ValueError("repo_url must start with http:// or https://")
        return v.rstrip("/")


# ── Response Models ────────────────────────────────────────────────────────────

class IssueResult(BaseModel):
    """A single code issue found during review."""
    title: str
    severity: str           # LOW / MEDIUM / HIGH / CRITICAL
    category: str           # security / style / complexity / logic / performance
    line_number: Optional[int]
    description: str
    suggestion: str
    source_tool: str        # ruff / bandit / radon / agent_reasoning
    confidence: float       # 0.0 to 1.0


class FileReviewResult(BaseModel):
    """Review results for one file."""
    file_path: str
    status: str             # completed / failed / max_steps_reached
    risk_score: float
    risk_label: str         # HIGH / MEDIUM / LOW / UNKNOWN
    steps_taken: int
    elapsed_seconds: float
    issues: list[IssueResult]
    final_review: str
    summary: str


class ReviewResponse(BaseModel):
    """Full response from a completed review session."""
    session_id: str
    repo_url: str
    started_at: str         # ISO 8601
    completed_at: Optional[str]
    files_reviewed: int
    total_issues: int
    critical_issues: int
    high_issues: int
    medium_issues: int
    low_issues: int
    patterns_detected: int
    total_elapsed_seconds: float
    file_results: list[FileReviewResult]
    repo_summary: str
    errors: list[str]


# ── Memory Models ──────────────────────────────────────────────────────────────

class MemoryStatsResponse(BaseModel):
    """Neo4j graph statistics."""
    connected: bool
    node_counts: dict[str, int]     # {"Repo": 1, "File": 9, "Issue": 3, ...}
    total_relationships: int
    total_reviews: int
    total_issues: int
    total_patterns: int


class FileMemoryResponse(BaseModel):
    """Memory history for a specific file."""
    file_path: str
    repo_url: str
    review_count: int
    avg_risk_score: float
    past_issues: list[dict]
    past_reviews: list[dict]


class PatternResponse(BaseModel):
    """Detected cross-file patterns for a repository."""
    patterns: list[dict]
    total: int


# ── Health Models ──────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    """API health status."""
    status: str             # healthy / degraded / unhealthy
    version: str = "1.0.0"
    neo4j_connected: bool
    groq_connected: bool
    uptime_seconds: float
    total_reviews_this_session: int