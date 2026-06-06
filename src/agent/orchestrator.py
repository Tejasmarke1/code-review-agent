"""
Multi-File Review Orchestrator
================================
"""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from loguru import logger
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import TOOLS, DEFECT_API
from src.agent.react_loop import ReActLoop
from src.agent.state import AgentState, AgentStatus
from src.agent.prompt_engine import PromptEngine
from src.llm.groq_client import GroqClient
from src.memory.neo4j_client import Neo4jClient
from src.memory.memory_writer import MemoryWriter
from src.memory.memory_retriever import MemoryRetriever
from src.tools.registry import ToolRegistry
from src.tools.file_tools import register_file_tools
from src.tools.analysis_tools import register_analysis_tools
from src.tools.defect_api_tool import register_defect_api_tools
from src.tools.memory_tools import register_memory_tools


@dataclass
class OrchestratorSession:
    """Complete results from a multi-file review session."""
    session_id:             str
    repo_url:               str
    started_at:             datetime
    completed_at:           Optional[datetime]       = None
    files_reviewed:         list[str]                = field(default_factory=list)
    file_states:            list[AgentState]         = field(default_factory=list)
    total_issues:           int                      = 0
    critical_issues:        int                      = 0
    high_issues:            int                      = 0
    medium_issues:          int                      = 0
    low_issues:             int                      = 0
    patterns_detected:      int                      = 0
    total_elapsed_seconds:  float                    = 0.0
    repo_summary:           str                      = ""
    errors:                 list[str]                = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class ReviewOrchestrator:
    """Coordinates multi-file review sessions with optional SSE streaming."""

    def __init__(
        self,
        use_memory: bool = True,
        inter_file_cooldown_seconds: int = 8,
    ) -> None:
        self.groq          = GroqClient()
        self.prompt_engine = PromptEngine()
        self.use_memory    = use_memory
        self._inter_file_cooldown = inter_file_cooldown_seconds

        self._on_step_callback:          Optional[Callable] = None
        self._on_issue_callback:         Optional[Callable] = None
        self._on_file_complete_callback: Optional[Callable] = None

        self.neo4j    = Neo4jClient.get_instance()
        self.writer:    Optional[MemoryWriter]    = None
        self.retriever: Optional[MemoryRetriever] = None

        if use_memory:
            connected = self.neo4j.connect()
            if connected:
                self.neo4j.initialize_schema()
                self.writer    = MemoryWriter(self.neo4j)
                self.retriever = MemoryRetriever(self.neo4j)
                logger.success("Memory layer ready (Neo4j connected)")
            else:
                logger.warning("Neo4j unavailable — running without memory")
                self.use_memory = False

    def review_repo(
        self,
        repo_url: str,
        files_to_review: Optional[list[str]] = None,
        use_defect_api: bool = True,
        max_files: int = 10,
    ) -> OrchestratorSession:
        """Review multiple files in a repository."""
        session = OrchestratorSession(
            session_id=str(uuid.uuid4())[:8],
            repo_url=repo_url,
            started_at=datetime.now(),
        )
        logger.info(f"[{session.session_id}] Starting repo review: {repo_url}")

        risk_scores:   dict[str, float] = {}
        risk_labels:   dict[str, str]   = {}
        shap_features: dict[str, list]  = {}

        if use_defect_api and files_to_review is None:
            risk_data      = self._get_risk_scores_from_api(repo_url)
            risk_scores    = risk_data.get("scores", {})
            risk_labels    = risk_data.get("labels", {})
            shap_features  = risk_data.get("shap", {})
            files_to_review = list(risk_scores.keys())[:max_files]

        if not files_to_review:
            logger.warning("No files to review.")
            session.errors.append("No files identified for review")
            session.completed_at = datetime.now()
            session.repo_summary = "No files were reviewed."
            return session

        logger.info(
            f"[{session.session_id}] Reviewing "
            f"{min(len(files_to_review), max_files)} files"
        )

        for idx, file_path in enumerate(files_to_review[:max_files]):
            if idx > 0 and self._inter_file_cooldown > 0:
                logger.info(
                    f"Cooling down {self._inter_file_cooldown}s before "
                    f"next file to let Groq token bucket refill..."
                )
                time.sleep(self._inter_file_cooldown)

            try:
                state = self._review_single_file(
                    file_path=file_path,
                    repo_url=repo_url,
                    risk_score=risk_scores.get(file_path, 0.0),
                    risk_label=risk_labels.get(file_path, "UNKNOWN"),
                    shap_features_for_file=shap_features.get(file_path, []),
                )
                session.file_states.append(state)
                session.files_reviewed.append(file_path)

                session.total_issues         += len(state.issues_found)
                session.total_elapsed_seconds += state.elapsed_seconds
                for issue in state.issues_found:
                    sev = issue.severity.value
                    if   sev == "CRITICAL": session.critical_issues += 1
                    elif sev == "HIGH":     session.high_issues     += 1
                    elif sev == "MEDIUM":   session.medium_issues   += 1
                    else:                   session.low_issues      += 1

                if self._on_file_complete_callback:
                    self._on_file_complete_callback({
                        "file_path":    file_path,
                        "issues_count": len(state.issues_found),
                        "status":       state.status.value,
                        "elapsed":      state.elapsed_seconds,
                    })

            except Exception as e:
                error_msg = f"Failed to review {file_path}: {e}"
                logger.error(error_msg)
                session.errors.append(error_msg)
                continue

        session.completed_at = datetime.now()
        session.repo_summary = self._generate_repo_summary(session)
        logger.success(
            f"[{session.session_id}] Session complete — "
            f"{len(session.files_reviewed)} files, "
            f"{session.total_issues} issues, "
            f"{session.total_elapsed_seconds:.1f}s"
        )
        return session

    def _review_single_file(
        self,
        file_path: str,
        repo_url: str,
        risk_score: float,
        risk_label: str,
        shap_features_for_file: list,
    ) -> AgentState:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_content = path.read_text(encoding="utf-8", errors="replace")

        past_context = ""
        if self.use_memory and self.retriever:
            past_context = self.retriever.get_file_context(file_path, repo_url)
            if past_context:
                logger.info(
                    f"Retrieved memory context for {path.name} "
                    f"({len(past_context)} chars)"
                )

        registry = ToolRegistry()
        register_file_tools(registry)
        register_analysis_tools(registry)
        register_defect_api_tools(registry)
        if self.use_memory:
            register_memory_tools(registry)

        try:
            from src.tools.github_tools import register_github_tools
            register_github_tools(registry)
        except Exception:
            pass

        loop = ReActLoop(self.groq, registry, self.prompt_engine)

        if self._on_step_callback or self._on_issue_callback:
            loop._streaming_step_cb  = self._on_step_callback
            loop._streaming_issue_cb = self._on_issue_callback

        state = loop.run(
            file_path=file_path,
            repo_url=repo_url,
            file_content=file_content,
            risk_score=risk_score,
            risk_label=risk_label,
            shap_features=shap_features_for_file,
            past_issues_context=past_context,
        )

        if (
            self.use_memory
            and self.writer
            and state.status in (AgentStatus.COMPLETED, AgentStatus.MAX_STEPS_REACHED)
        ):
            result = self.writer.write_review(state)
            if result.get("patterns_detected", 0) > 0:
                logger.info(f"New patterns detected: {result['patterns_detected']}")

        return state

    def _get_risk_scores_from_api(self, repo_url: str) -> dict:
        import requests
        try:
            resp = requests.post(
                f"{DEFECT_API['base_url']}/analyze",
                json={
                    "repo_url":   repo_url,
                    "top_k":      TOOLS["max_files_per_review"],
                    "use_hybrid": DEFECT_API["use_hybrid"],
                },
                timeout=DEFECT_API["timeout_seconds"],
            )
            resp.raise_for_status()
            data = resp.json()
            scores, labels, shap = {}, {}, {}
            for r in data.get("top_k_results", []):
                fp         = r["file_path"]
                scores[fp] = r["risk_score"]
                labels[fp] = r["risk_label"]
                shap[fp]   = r.get("top_shap_features", [])
            return {"scores": scores, "labels": labels, "shap": shap}
        except Exception as e:
            logger.warning(f"Defect API unavailable: {e}")
            return {"scores": {}, "labels": {}, "shap": {}}

    def _generate_repo_summary(self, session: OrchestratorSession) -> str:
        if not session.file_states:
            return "No files were successfully reviewed."

        repo_name  = session.repo_url.rstrip("/").split("/")[-1]
        issue_rows = []
        for state in session.file_states:
            for issue in state.issues_found:
                issue_rows.append(
                    f"| `{Path(state.file_path).name}` "
                    f"| **{issue.severity.value}** "
                    f"| {issue.category} "
                    f"| {issue.title[:60]} "
                    f"| {issue.source_tool} |"
                )

        issues_table = (
            "| File | Severity | Category | Issue | Tool |\n"
            "|------|----------|----------|-------|------|\n"
            + "\n".join(issue_rows)
        ) if issue_rows else "_No issues found._"

        file_lines = []
        for state in session.file_states:
            counts   = state.issue_count_by_severity
            non_zero = {k: v for k, v in counts.items() if v > 0}
            sev_str  = ", ".join(f"{k}={v}" for k, v in non_zero.items()) or "none"
            file_lines.append(
                f"| `{Path(state.file_path).name}` "
                f"| {state.risk_score:.2f} "
                f"| {state.risk_label} "
                f"| {state.status.value} "
                f"| {sev_str} |"
            )

        file_table = (
            "| File | Risk | Label | Status | Issues |\n"
            "|------|------|-------|--------|--------|\n"
            + "\n".join(file_lines)
        )

        errors_section = (
            "\n## Errors\n\n" + "\n".join(f"- {e}" for e in session.errors)
        ) if session.errors else ""

        return f"""# Code Review Report — {repo_name}

**Session:** `{session.session_id}`
**Date:** {session.started_at.strftime('%Y-%m-%d %H:%M UTC')}
**Repository:** {session.repo_url}
**Total time:** {session.total_elapsed_seconds:.1f}s

## Headline Metrics

| Metric | Count |
|--------|-------|
| Files reviewed | {len(session.files_reviewed)} |
| Total issues | {session.total_issues} |
| Critical | {session.critical_issues} |
| High | {session.high_issues} |
| Medium | {session.medium_issues} |
| Low | {session.low_issues} |

## Per-File Summary

{file_table}

## All Issues

{issues_table}
{errors_section}
"""