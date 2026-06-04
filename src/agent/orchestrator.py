"""
Multi-File Review Orchestrator
================================
Coordinates reviewing multiple files in one session.

Responsibilities:
1. Accept a repo URL + list of files to review
2. For each file: retrieve memory context → run ReActLoop → write memory
3. Pass patterns discovered in earlier files to later file reviews
4. Produce a consolidated repo-level report
5. Track session-level statistics

This is what gets called from the API (Day 4) and CLI scripts.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
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
    session_id: str
    repo_url: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    files_reviewed: list[str] = field(default_factory=list)
    file_states: list[AgentState] = field(default_factory=list)
    total_issues: int = 0
    critical_issues: int = 0
    high_issues: int = 0
    medium_issues: int = 0
    low_issues: int = 0
    patterns_detected: int = 0
    total_elapsed_seconds: float = 0.0
    repo_summary: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        """Total wall-clock time for the session."""
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()


class ReviewOrchestrator:
    """
    Coordinates multi-file review sessions.

    Usage::

        orchestrator = ReviewOrchestrator(use_memory=True)
        session = orchestrator.review_repo(
            repo_url="https://github.com/pallets/flask",
            files_to_review=["src/flask/app.py", "src/flask/routing.py"],
        )
        print(session.repo_summary)

    When ``use_memory=False`` (or when Neo4j is unavailable) the orchestrator
    degrades gracefully — reviews still run, memory context is simply absent.
    """

    def __init__(self, use_memory: bool = True) -> None:
        """Initialise the orchestrator and optionally connect to Neo4j.

        Args:
            use_memory: If True, attempt to connect to Neo4j and use graph
                memory for context retrieval and result persistence.  If the
                connection fails the orchestrator continues without memory.
        """
        self.groq = GroqClient()
        self.prompt_engine = PromptEngine()
        self.use_memory = use_memory

        # Memory layer initialisation
        self.neo4j = Neo4jClient.get_instance()
        self.writer: Optional[MemoryWriter] = None
        self.retriever: Optional[MemoryRetriever] = None

        if use_memory:
            connected = self.neo4j.connect()
            if connected:
                self.neo4j.initialize_schema()
                self.writer = MemoryWriter(self.neo4j)
                self.retriever = MemoryRetriever(self.neo4j)
                logger.success("Memory layer ready (Neo4j connected)")
            else:
                logger.warning(
                    "Neo4j unavailable — running without memory. "
                    "Reviews still work; cross-session context disabled."
                )
                self.use_memory = False

    def review_repo(
        self,
        repo_url: str,
        files_to_review: Optional[list[str]] = None,
        use_defect_api: bool = True,
        max_files: int = 10,
    ) -> OrchestratorSession:
        """Review multiple files in a repository in a single orchestrated session.

        File selection priority:
        1. Use ``files_to_review`` if explicitly provided.
        2. Otherwise call the Defect Prediction API for top-k risky files.
        3. Fall back to listing all Python files if the API is unavailable.

        Files are reviewed in descending risk-score order.  Memory context is
        retrieved before each file review and written to the graph afterwards.

        Args:
            repo_url: GitHub or other VCS URL of the repository.
            files_to_review: Explicit list of absolute file paths, or None for
                auto-detection via the Defect API.
            use_defect_api: If True, call the Defect Prediction API when
                ``files_to_review`` is None.
            max_files: Hard cap on the number of files reviewed per session.

        Returns:
            Completed :class:`OrchestratorSession` with all per-file results,
            aggregated counts, and a markdown repo summary.
        """
        session = OrchestratorSession(
            session_id=str(uuid.uuid4())[:8],
            repo_url=repo_url,
            started_at=datetime.now(),
        )
        logger.info(f"[{session.session_id}] Starting repo review: {repo_url}")

        # ── Risk score data from Defect Prediction API ─────────────────────────
        risk_scores: dict[str, float] = {}
        risk_labels: dict[str, str] = {}
        shap_features: dict[str, list] = {}

        if use_defect_api and files_to_review is None:
            risk_data = self._get_risk_scores_from_api(repo_url)
            risk_scores = risk_data.get("scores", {})
            risk_labels = risk_data.get("labels", {})
            shap_features = risk_data.get("shap", {})
            files_to_review = list(risk_scores.keys())[:max_files]

        if not files_to_review:
            logger.warning(
                "No files to review. "
                "Provide files_to_review or ensure Defect API is running."
            )
            session.errors.append("No files identified for review")
            session.completed_at = datetime.now()
            session.repo_summary = "No files were reviewed."
            return session

        logger.info(
            f"[{session.session_id}] Reviewing {min(len(files_to_review), max_files)} files"
        )

        # ── Review each file ───────────────────────────────────────────────────
        for file_path in files_to_review[:max_files]:
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

                # Accumulate counts
                session.total_issues += len(state.issues_found)
                session.total_elapsed_seconds += state.elapsed_seconds
                for issue in state.issues_found:
                    sev = issue.severity.value
                    if sev == "CRITICAL":
                        session.critical_issues += 1
                    elif sev == "HIGH":
                        session.high_issues += 1
                    elif sev == "MEDIUM":
                        session.medium_issues += 1
                    else:
                        session.low_issues += 1

            except Exception as e:
                error_msg = f"Failed to review {file_path}: {e}"
                logger.error(error_msg)
                session.errors.append(error_msg)
                continue

        # ── Finalise ───────────────────────────────────────────────────────────
        session.completed_at = datetime.now()
        session.repo_summary = self._generate_repo_summary(session)

        logger.success(
            f"[{session.session_id}] Session complete — "
            f"{len(session.files_reviewed)} files, "
            f"{session.total_issues} issues, "
            f"{session.total_elapsed_seconds:.1f}s"
        )
        return session

    # ── Private helpers ────────────────────────────────────────────────────────

    def _review_single_file(
        self,
        file_path: str,
        repo_url: str,
        risk_score: float,
        risk_label: str,
        shap_features_for_file: list,
    ) -> AgentState:
        """Run a complete review for one file.

        Workflow:
        1. Read file content from disk.
        2. Retrieve memory context from Neo4j (if available).
        3. Build a fresh ToolRegistry (stateless per review).
        4. Run the ReActLoop.
        5. Write results to Neo4j (if available and review completed).

        Args:
            file_path: Absolute path to the Python file.
            repo_url: Repository URL for context.
            risk_score: ML risk score (0.0–1.0).
            risk_label: Human-readable risk label (HIGH / MEDIUM / LOW / UNKNOWN).
            shap_features_for_file: SHAP feature list from the ML model.

        Returns:
            Completed AgentState.

        Raises:
            FileNotFoundError: If the file does not exist on disk.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_content = path.read_text(encoding="utf-8", errors="replace")

        # Retrieve memory context for this file
        past_context = ""
        if self.use_memory and self.retriever:
            past_context = self.retriever.get_file_context(file_path, repo_url)
            if past_context:
                logger.info(
                    f"Retrieved memory context for {path.name} "
                    f"({len(past_context)} chars)"
                )

        # Fresh registry per review (prevents state bleed between files)
        registry = ToolRegistry()
        register_file_tools(registry)
        register_analysis_tools(registry)
        register_defect_api_tools(registry)
        if self.use_memory:
            register_memory_tools(registry)

        # Run ReAct loop
        loop = ReActLoop(self.groq, registry, self.prompt_engine)
        state = loop.run(
            file_path=file_path,
            repo_url=repo_url,
            file_content=file_content,
            risk_score=risk_score,
            risk_label=risk_label,
            shap_features=shap_features_for_file,
            past_issues_context=past_context,
        )

        # Persist to graph memory
        if (
            self.use_memory
            and self.writer
            and state.status in (AgentStatus.COMPLETED, AgentStatus.MAX_STEPS_REACHED)
        ):
            result = self.writer.write_review(state)
            if result.get("patterns_detected", 0) > 0:
                logger.info(
                    f"New patterns detected: {result['patterns_detected']}"
                )

        return state

    def _get_risk_scores_from_api(self, repo_url: str) -> dict:
        """Call the Defect Prediction API and parse its risk score payload.

        Falls back gracefully to an empty dict if the API is unreachable so
        the orchestrator can continue without ML context.

        Args:
            repo_url: Repository URL to submit for analysis.

        Returns:
            Dict with keys ``"scores"``, ``"labels"``, ``"shap"`` — each a
            ``dict[file_path, value]``.  All dicts are empty on API failure.
        """
        import requests

        try:
            resp = requests.post(
                f"{DEFECT_API['base_url']}/analyze",
                json={
                    "repo_url": repo_url,
                    "top_k": TOOLS["max_files_per_review"],
                    "use_hybrid": DEFECT_API["use_hybrid"],
                },
                timeout=DEFECT_API["timeout_seconds"],
            )
            resp.raise_for_status()
            data = resp.json()

            scores: dict[str, float] = {}
            labels: dict[str, str] = {}
            shap:   dict[str, list] = {}
            for result in data.get("top_k_results", []):
                fp = result["file_path"]
                scores[fp] = result["risk_score"]
                labels[fp] = result["risk_label"]
                shap[fp]   = result.get("top_shap_features", [])

            logger.info(
                f"Defect API returned {len(scores)} files for {repo_url}"
            )
            return {"scores": scores, "labels": labels, "shap": shap}

        except Exception as e:
            logger.warning(f"Defect Prediction API unavailable: {e}")
            return {"scores": {}, "labels": {}, "shap": {}}

    def _generate_repo_summary(self, session: OrchestratorSession) -> str:
        """Generate a complete markdown repo-level summary from all file reviews.

        Args:
            session: The completed OrchestratorSession.

        Returns:
            Markdown string covering headline metrics, a full issues table,
            and a per-file breakdown.
        """
        if not session.file_states:
            return "No files were successfully reviewed."

        repo_name = session.repo_url.rstrip("/").split("/")[-1]

        # Build issues table rows
        issue_rows: list[str] = []
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

        # Per-file breakdown
        file_lines: list[str] = []
        for state in session.file_states:
            counts = state.issue_count_by_severity
            non_zero = {k: v for k, v in counts.items() if v > 0}
            sev_str = ", ".join(f"{k}={v}" for k, v in non_zero.items()) or "none"
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
        ) if file_lines else "_No files reviewed._"

        errors_section = ""
        if session.errors:
            errors_section = (
                "\n## Errors\n\n"
                + "\n".join(f"- {e}" for e in session.errors)
            )

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