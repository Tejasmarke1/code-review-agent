"""
Report Generator
================
Saves review results to disk as markdown and JSON.
Called after each session by the orchestrator or CLI.

Output structure::

    reports/{repo_name}/{session_id}/
        summary.md      — human-readable markdown report
        findings.json   — machine-readable full results
        trace.md        — full agent ReAct trace (for debugging and demos)
"""

import json
from datetime import datetime
from pathlib import Path
from loguru import logger
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))
from configs.config import OUTPUT
from src.agent.state import AgentState, AgentStatus
from src.agent.orchestrator import OrchestratorSession


class ReportGenerator:
    """Persists OrchestratorSession results to a structured directory on disk."""

    def __init__(self) -> None:
        self.reports_dir: Path = OUTPUT["reports_dir"]

    def save_session_report(self, session: OrchestratorSession) -> Path:
        """Save a complete session report to disk.

        Creates a dated directory under ``reports/{repo_name}/{session_id}/``
        containing three files:
        - ``summary.md``   — human-readable markdown.
        - ``findings.json``— machine-readable JSON with all issues and metadata.
        - ``trace.md``     — full ReAct trace of every agent step.

        Args:
            session: Completed :class:`OrchestratorSession`.

        Returns:
            Path to the report directory that was created.
        """
        repo_name = session.repo_url.rstrip("/").split("/")[-1]
        report_dir = self.reports_dir / repo_name / session.session_id
        report_dir.mkdir(parents=True, exist_ok=True)

        # 1. Summary markdown
        summary_path = report_dir / "summary.md"
        summary_path.write_text(session.repo_summary, encoding="utf-8")

        # 2. Machine-readable JSON
        findings_path = report_dir / "findings.json"
        findings_data = self._build_findings_dict(session)
        findings_path.write_text(
            json.dumps(findings_data, indent=2, default=str),
            encoding="utf-8",
        )

        # 3. Agent trace markdown
        trace_path = report_dir / "trace.md"
        trace_path.write_text(
            self._build_trace_markdown(session),
            encoding="utf-8",
        )

        logger.success(
            f"Report saved → {report_dir}  "
            f"({session.total_issues} issues across {len(session.files_reviewed)} files)"
        )
        return report_dir

    def save_single_file_report(
        self,
        state: AgentState,
        repo_url: str,
        session_id: str,
    ) -> Path:
        """Save a single-file review result when no orchestrator session exists.

        Useful for one-off reviews from the CLI or API without the full
        orchestrator wrapper.

        Args:
            state: Completed AgentState from ReActLoop.run().
            repo_url: Repository URL for directory naming.
            session_id: Unique session identifier.

        Returns:
            Path to the report directory.
        """
        repo_name = repo_url.rstrip("/").split("/")[-1]
        report_dir = self.reports_dir / repo_name / session_id
        report_dir.mkdir(parents=True, exist_ok=True)

        # Findings JSON
        findings = {
            "session_id":   session_id,
            "repo_url":     repo_url,
            "file_path":    state.file_path,
            "status":       state.status.value,
            "risk_score":   state.risk_score,
            "risk_label":   state.risk_label,
            "steps_taken":  state.current_step,
            "elapsed_seconds": state.elapsed_seconds,
            "issues": [
                {
                    "title":       i.title,
                    "severity":    i.severity.value,
                    "category":    i.category,
                    "line_number": i.line_number,
                    "description": i.description,
                    "suggestion":  i.suggestion,
                    "source_tool": i.source_tool,
                    "confidence":  i.confidence,
                }
                for i in state.issues_found
            ],
            "summary": state.summary,
            "final_review": state.final_review,
        }
        (report_dir / "findings.json").write_text(
            json.dumps(findings, indent=2, default=str), encoding="utf-8"
        )
        (report_dir / "review.md").write_text(
            state.final_review or "_No review generated._",
            encoding="utf-8",
        )

        logger.success(f"Single-file report saved → {report_dir}")
        return report_dir

    # ── Private helpers ────────────────────────────────────────────────────────

    def _build_findings_dict(self, session: OrchestratorSession) -> dict:
        """Serialise the full session to a plain Python dict for JSON output.

        Args:
            session: Completed OrchestratorSession.

        Returns:
            JSON-serialisable dict.
        """
        return {
            "session_id":    session.session_id,
            "repo_url":      session.repo_url,
            "started_at":    session.started_at.isoformat(),
            "completed_at":  (
                session.completed_at.isoformat()
                if session.completed_at
                else None
            ),
            "total_files":          len(session.files_reviewed),
            "total_issues":         session.total_issues,
            "critical_issues":      session.critical_issues,
            "high_issues":          session.high_issues,
            "medium_issues":        session.medium_issues,
            "low_issues":           session.low_issues,
            "patterns_detected":    session.patterns_detected,
            "total_elapsed_seconds": session.total_elapsed_seconds,
            "files": [
                {
                    "file_path":       state.file_path,
                    "risk_score":      state.risk_score,
                    "risk_label":      state.risk_label,
                    "status":          state.status.value,
                    "steps_taken":     state.current_step,
                    "elapsed_seconds": state.elapsed_seconds,
                    "tools_called":    sorted(set(state.tools_called)),
                    "issues": [
                        {
                            "title":       i.title,
                            "severity":    i.severity.value,
                            "category":    i.category,
                            "line_number": i.line_number,
                            "description": i.description,
                            "suggestion":  i.suggestion,
                            "source_tool": i.source_tool,
                            "confidence":  i.confidence,
                        }
                        for i in state.issues_found
                    ],
                    "summary":      state.summary,
                    "final_review": state.final_review,
                }
                for state in session.file_states
            ],
            "errors": session.errors,
        }

    def _build_trace_markdown(self, session: OrchestratorSession) -> str:
        """Build a human-readable markdown trace of all agent steps.

        Each file gets its own section; each step shows Thought / Action /
        Input / Observation.  Observations are capped at 500 chars to keep
        the file readable.

        Args:
            session: Completed OrchestratorSession.

        Returns:
            Markdown string.
        """
        repo_name = session.repo_url.rstrip("/").split("/")[-1]
        lines = [
            f"# Agent Trace — {repo_name}",
            f"",
            f"**Session:** `{session.session_id}`  ",
            f"**Date:** {session.started_at.strftime('%Y-%m-%d %H:%M')}  ",
            f"**Files reviewed:** {len(session.files_reviewed)}  ",
            f"",
        ]

        for state in session.file_states:
            file_name = Path(state.file_path).name
            lines += [
                f"---",
                f"",
                f"## {file_name}",
                f"",
                f"**Path:** `{state.file_path}`  ",
                f"**Risk:** {state.risk_score:.3f} ({state.risk_label})  ",
                f"**Status:** {state.status.value}  ",
                f"**Steps:** {state.current_step}  ",
                f"",
            ]

            if not state.thought_history:
                lines.append("_No steps recorded._\n")
                continue

            for step in state.thought_history:
                obs_preview = step.observation[:500]
                if len(step.observation) > 500:
                    obs_preview += "\n... [truncated]"

                lines += [
                    f"### Step {step.step_number} — `{step.action}`",
                    f"",
                    f"**Thought:**",
                    f"> {step.thought.strip()}",
                    f"",
                    f"**Action Input:** `{step.action_input}`",
                    f"",
                    f"**Observation:**",
                    f"```",
                    obs_preview,
                    f"```",
                    f"",
                ]

            if state.final_review:
                lines += [
                    f"### Final Review",
                    f"",
                    state.final_review,
                    f"",
                ]

        return "\n".join(lines)