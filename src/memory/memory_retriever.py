"""
Memory Retriever
================
Retrieves relevant past review context from Neo4j for the agent.

Called BEFORE each file review to give the agent:
- Past issues in this specific file
- Similar issues across the repo
- Active patterns affecting this file or repo
- Repo-level statistics

The output is a plain-text context string injected into the agent prompt.
All queries are scoped to ``repo_url`` — no cross-repo data leakage.
"""

from pathlib import Path
from loguru import logger
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.memory.neo4j_client import Neo4jClient


class MemoryRetriever:
    """
    Retrieves structured context from Neo4j for agent prompts.

    All queries are scoped to ``repo_url`` — never leaks data between repos.
    Every method returns a gracefully formatted string even when the graph
    is empty or Neo4j is unavailable.
    """

    def __init__(self, client: Neo4jClient) -> None:
        """Initialise the retriever with an already-connected Neo4jClient.

        Args:
            client: An active Neo4jClient instance.
        """
        self.client = client

    def get_file_context(self, file_path: str, repo_url: str) -> str:
        """Return all relevant memory context for a file before reviewing it.

        This is the primary method called by the orchestrator.  It assembles
        context from four sub-queries and returns a single formatted string
        ready to be injected into the agent's initial prompt.

        Returns an empty string if Neo4j is not connected or the graph contains
        no history for this file/repo — the agent can always proceed without it.

        Args:
            file_path: Absolute or relative path of the file to be reviewed.
            repo_url: Repository URL used to scope all queries.

        Returns:
            Formatted multi-section context string, or ``""`` if unavailable.
        """
        if not self.client.is_connected:
            return ""

        try:
            sections = []

            past_issues = self._get_past_issues_for_file(file_path, repo_url)
            if past_issues:
                sections.append(past_issues)

            patterns = self._get_active_patterns(repo_url)
            if patterns:
                sections.append(patterns)

            repo_stats = self._get_repo_stats(repo_url)
            if repo_stats:
                sections.append(repo_stats)

            recurring = self._get_recurring_issues_in_repo(file_path, repo_url)
            if recurring:
                sections.append(recurring)

            if not sections:
                return ""

            return "=== MEMORY CONTEXT (from past reviews) ===\n" + "\n\n".join(sections)

        except Exception as e:
            logger.warning(f"Memory retrieval failed (non-fatal): {e}")
            return ""

    # ── Private helpers ────────────────────────────────────────────────────────

    def _get_past_issues_for_file(self, file_path: str, repo_url: str) -> str:
        """Return all issues previously found in this specific file.

        Sorted by severity (CRITICAL first) then by ``occurrence_count`` descending.
        Capped at 10 results.

        Args:
            file_path: Path of the file to query.
            repo_url: Repository URL filter.

        Returns:
            Formatted string, or ``""`` if no past issues exist.
        """
        results = self.client.query(
            """
            MATCH (f:File {path: $file_path, repo_url: $repo_url})-[:HAS_ISSUE]->(i:Issue)
            RETURN i.title           AS title,
                   i.severity        AS severity,
                   i.category        AS category,
                   i.source_tool     AS tool,
                   i.line_number     AS line,
                   i.occurrence_count AS occurrences,
                   i.last_seen       AS last_seen
            ORDER BY
                CASE i.severity
                    WHEN 'CRITICAL' THEN 0
                    WHEN 'HIGH'     THEN 1
                    WHEN 'MEDIUM'   THEN 2
                    ELSE 3
                END,
                i.occurrence_count DESC
            LIMIT 10
            """,
            {"file_path": file_path, "repo_url": repo_url},
        )

        if not results:
            return ""

        lines = [f"Past issues in {Path(file_path).name} ({len(results)} found):"]
        for r in results:
            recurrence = f" (seen {r['occurrences']}x)" if r.get("occurrences", 1) > 1 else ""
            lines.append(
                f"  [{r['severity']}] {r['title']} — "
                f"line {r['line']}, found by {r['tool']}{recurrence}"
            )
        return "\n".join(lines)

    def _get_active_patterns(self, repo_url: str) -> str:
        """Return all active cross-file patterns detected in this repo.

        Patterns signal systemic problems the agent should specifically probe for.
        Sorted by severity, capped at 5.

        Args:
            repo_url: Repository URL filter.

        Returns:
            Formatted string, or ``""`` if no patterns exist.
        """
        results = self.client.query(
            """
            MATCH (p:Pattern)-[:AFFECTS]->(f:File {repo_url: $repo_url})
            WITH p, count(f) AS affected_count
            RETURN p.description AS description,
                   p.category    AS category,
                   p.severity    AS severity,
                   affected_count
            ORDER BY
                CASE p.severity
                    WHEN 'CRITICAL' THEN 0
                    WHEN 'HIGH'     THEN 1
                    WHEN 'MEDIUM'   THEN 2
                    ELSE 3
                END
            LIMIT 5
            """,
            {"repo_url": repo_url},
        )

        if not results:
            return ""

        lines = ["Active patterns in this repository (look for these specifically):"]
        for r in results:
            lines.append(
                f"  [{r['severity']}] {r['description']} "
                f"(affects {r['affected_count']} files)"
            )
        return "\n".join(lines)

    def _get_repo_stats(self, repo_url: str) -> str:
        """Return high-level repo review statistics as a single summary line.

        Args:
            repo_url: Repository URL filter.

        Returns:
            Formatted string, or ``""`` if the repo has not been reviewed yet.
        """
        result = self.client.query_single(
            """
            MATCH (r:Repo {url: $repo_url})
            OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
            OPTIONAL MATCH (f)-[:HAS_ISSUE]->(i:Issue)
            RETURN r.total_reviews     AS total_reviews,
                   count(DISTINCT f)   AS files_reviewed,
                   count(i)            AS total_issues,
                   avg(f.avg_risk_score) AS avg_risk
            """,
            {"repo_url": repo_url},
        )

        if not result or not result.get("total_reviews"):
            return ""

        avg_risk = result.get("avg_risk") or 0.0
        return (
            f"Repository history: "
            f"{result['total_reviews']} reviews, "
            f"{result['files_reviewed']} files, "
            f"{result['total_issues']} total issues found, "
            f"avg risk score: {avg_risk:.3f}"
        )

    def _get_recurring_issues_in_repo(self, current_file: str, repo_url: str) -> str:
        """Return issue categories seen in 2+ files other than the current one.

        Signals systemic problems the agent should probe for in the current file.

        Args:
            current_file: Path of the file currently being reviewed (excluded from results).
            repo_url: Repository URL filter.

        Returns:
            Formatted string, or ``""`` if no cross-file patterns exist yet.
        """
        results = self.client.query(
            """
            MATCH (i:Issue {repo_url: $repo_url})
            WHERE i.file_path <> $current_file
            WITH i.category AS category,
                 i.severity  AS severity,
                 collect(DISTINCT i.file_path) AS other_files,
                 count(DISTINCT i.file_path)   AS file_count
            WHERE file_count >= 2
            RETURN category, severity, file_count
            ORDER BY file_count DESC
            LIMIT 5
            """,
            {"repo_url": repo_url, "current_file": current_file},
        )

        if not results:
            return ""

        lines = ["Recurring issue types seen in other files (check for these):"]
        for r in results:
            lines.append(
                f"  [{r['severity']}] {r['category']} issues "
                f"seen in {r['file_count']} other files"
            )
        return "\n".join(lines)

    def get_similar_files(
        self, file_path: str, repo_url: str, limit: int = 3
    ) -> list[str]:
        """Return paths of files with similar issue profiles to the given file.

        Similarity is measured by the number of shared issue categories.

        Args:
            file_path: Path of the reference file.
            repo_url: Repository URL filter.
            limit: Maximum number of similar files to return.

        Returns:
            List of file path strings (may be empty).
        """
        results = self.client.query(
            """
            MATCH (f1:File {path: $file_path, repo_url: $repo_url})-[:HAS_ISSUE]->(i1:Issue)
            MATCH (f2:File {repo_url: $repo_url})-[:HAS_ISSUE]->(i2:Issue)
            WHERE f2.path <> $file_path
              AND i1.category = i2.category
            WITH f2.path AS similar_file, count(*) AS shared_categories
            ORDER BY shared_categories DESC
            LIMIT $limit
            RETURN similar_file
            """,
            {"file_path": file_path, "repo_url": repo_url, "limit": limit},
        )
        return [r["similar_file"] for r in results]