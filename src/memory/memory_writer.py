"""
Memory Writer
=============
Writes agent review results into the Neo4j graph.
Called after every completed AgentState.

Responsibilities:
- Create/update Repo and File nodes
- Create Review node for this session
- Create or deduplicate Issue nodes
- Link everything with correct relationships
- Detect and promote recurring patterns
"""

import json
import uuid
from datetime import datetime
from typing import Any
from loguru import logger
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.agent.state import AgentState, ReviewIssue, AgentStatus
from src.memory.neo4j_client import Neo4jClient
from src.memory.graph_schema import NodeLabel, RelType


class MemoryWriter:
    """
    Persists agent review results to Neo4j graph memory.

    Call ``write_review(state)`` after every completed review.
    The writer handles deduplication and pattern detection automatically.
    """

    # Minimum number of distinct files an issue category must appear in
    # before being promoted to a Pattern node.
    PATTERN_THRESHOLD = 3

    def __init__(self, client: Neo4jClient) -> None:
        """Initialise the writer with an already-connected Neo4jClient.

        Args:
            client: An active Neo4jClient instance.
        """
        self.client = client

    def write_review(self, state: AgentState) -> dict:
        """Persist a completed AgentState to Neo4j.

        Steps performed:
        1. Upsert Repo node (create or update ``last_reviewed``).
        2. Upsert File node (update running ``avg_risk_score``, ``review_count``).
        3. Create Review node linked to the File.
        4. For each issue: deduplicate by ``title + file_path + source_tool``,
           incrementing ``occurrence_count`` on matches rather than creating
           duplicate nodes.
        5. Create all graph relationships.
        6. Run pattern detection — promote issues in 3+ files to Pattern nodes.

        This method never raises; all exceptions are caught, logged, and
        returned in the result dict under the ``"error"`` key.

        Args:
            state: Completed AgentState from ``ReActLoop.run()``.

        Returns:
            Dict with keys:
            - ``nodes_created`` (int)
            - ``issues_written`` (int)
            - ``patterns_detected`` (int)
            - ``error`` (str, only present on failure)
        """
        if not self.client.is_connected:
            logger.warning("Neo4j not connected — skipping memory write")
            return {"nodes_created": 0, "issues_written": 0, "patterns_detected": 0}

        try:
            self._upsert_repo(state)
            self._upsert_file(state)
            review_id = self._create_review(state)
            issues_written = self._write_issues(state, review_id)
            patterns = self._detect_and_promote_patterns(state.repo_url)

            logger.success(
                f"Memory written for {Path(state.file_path).name}: "
                f"{issues_written} issues, {patterns} new patterns"
            )
            return {
                "nodes_created": 3 + issues_written,
                "issues_written": issues_written,
                "patterns_detected": patterns,
            }

        except Exception as e:
            logger.error(f"Memory write failed for {state.file_path}: {e}")
            return {
                "nodes_created": 0,
                "issues_written": 0,
                "patterns_detected": 0,
                "error": str(e),
            }

    # ── Private helpers ────────────────────────────────────────────────────────

    def _upsert_repo(self, state: AgentState) -> None:
        """Create Repo node if it does not exist; update ``last_reviewed`` and increment counter.

        Args:
            state: AgentState providing ``repo_url``.
        """
        repo_name = state.repo_url.rstrip("/").split("/")[-1]
        self.client.query(
            """
            MERGE (r:Repo {url: $url})
            ON CREATE SET
                r.name = $name,
                r.created_at = $now,
                r.total_reviews = 0
            ON MATCH SET
                r.last_reviewed = $now
            SET r.total_reviews = r.total_reviews + 1
            """,
            {
                "url": state.repo_url,
                "name": repo_name,
                "now": datetime.now().isoformat(),
            },
        )

    def _upsert_file(self, state: AgentState) -> None:
        """Create File node if it does not exist; update running average risk score.

        The ``avg_risk_score`` is maintained as an online average so it stays
        accurate across many reviews without fetching all historical values.

        Args:
            state: AgentState providing ``file_path``, ``repo_url``, and ``risk_score``.
        """
        self.client.query(
            """
            MATCH (r:Repo {url: $repo_url})
            MERGE (f:File {path: $path, repo_url: $repo_url})
            ON CREATE SET
                f.language = 'python',
                f.review_count = 0,
                f.avg_risk_score = $risk_score,
                f.lines_of_code = $loc,
                f.last_reviewed = $now
            ON MATCH SET
                f.avg_risk_score = (f.avg_risk_score * f.review_count + $risk_score)
                                   / (f.review_count + 1),
                f.last_reviewed = $now
            SET f.review_count = f.review_count + 1
            MERGE (r)-[:CONTAINS]->(f)
            """,
            {
                "repo_url": state.repo_url,
                "path": state.file_path,
                "risk_score": state.risk_score,
                "loc": len(state.file_content.splitlines()),
                "now": datetime.now().isoformat(),
            },
        )

    def _create_review(self, state: AgentState) -> str:
        """Create a Review node and link it to its parent File node.

        Args:
            state: AgentState for the completed review session.

        Returns:
            The ``session_id`` of the newly created Review node.
        """
        self.client.query(
            """
            MATCH (f:File {path: $path, repo_url: $repo_url})
            CREATE (rv:Review {
                session_id:     $session_id,
                file_path:      $path,
                repo_url:       $repo_url,
                timestamp:      $now,
                status:         $status,
                steps_taken:    $steps,
                tools_called:   $tools,
                risk_score:     $risk_score,
                risk_label:     $risk_label,
                issues_found:   $issues_found,
                elapsed_seconds: $elapsed
            })
            CREATE (f)-[:HAS_REVIEW]->(rv)
            """,
            {
                "session_id":  state.session_id,
                "path":        state.file_path,
                "repo_url":    state.repo_url,
                "now":         datetime.now().isoformat(),
                "status":      state.status.value,
                "steps":       state.current_step,
                "tools":       json.dumps(sorted(set(state.tools_called))),
                "risk_score":  state.risk_score,
                "risk_label":  state.risk_label,
                "issues_found": len(state.issues_found),
                "elapsed":     state.elapsed_seconds,
            },
        )
        return state.session_id

    def _write_issues(self, state: AgentState, review_id: str) -> int:
        """Write all issues from a review to the graph with deduplication.

        Deduplication key: ``title + file_path + source_tool``.
        - Match found  → increment ``occurrence_count``, update ``last_seen``.
        - No match     → create new Issue node and all relevant relationships.

        Args:
            state: AgentState containing ``issues_found``.
            review_id: ``session_id`` of the current Review node.

        Returns:
            Number of issues processed (created + updated).
        """
        written = 0
        for issue in state.issues_found:
            existing = self.client.query_single(
                """
                MATCH (i:Issue {
                    title:       $title,
                    file_path:   $file_path,
                    source_tool: $source_tool
                })
                RETURN i.issue_id AS issue_id
                """,
                {
                    "title":       issue.title,
                    "file_path":   state.file_path,
                    "source_tool": issue.source_tool,
                },
            )

            if existing:
                # Deduplicate — only update counters
                self.client.query(
                    """
                    MATCH (i:Issue {issue_id: $issue_id})
                    SET i.occurrence_count = i.occurrence_count + 1,
                        i.last_seen = $now
                    """,
                    {
                        "issue_id": existing["issue_id"],
                        "now": datetime.now().isoformat(),
                    },
                )
            else:
                # New issue — create node and wire up all relationships
                issue_id = str(uuid.uuid4())
                self.client.query(
                    """
                    MATCH (f:File {path: $file_path, repo_url: $repo_url})
                    MATCH (rv:Review {session_id: $session_id})
                    CREATE (i:Issue {
                        issue_id:        $issue_id,
                        title:           $title,
                        category:        $category,
                        severity:        $severity,
                        description:     $description,
                        suggestion:      $suggestion,
                        source_tool:     $source_tool,
                        line_number:     $line_number,
                        confidence:      $confidence,
                        file_path:       $file_path,
                        repo_url:        $repo_url,
                        first_seen:      $now,
                        last_seen:       $now,
                        occurrence_count: 1
                    })
                    CREATE (rv)-[:FOUND]->(i)
                    CREATE (f)-[:HAS_ISSUE]->(i)
                    """,
                    {
                        "issue_id":    issue_id,
                        "title":       issue.title,
                        "category":    issue.category,
                        "severity":    issue.severity.value,
                        "description": issue.description,
                        "suggestion":  issue.suggestion,
                        "source_tool": issue.source_tool,
                        "line_number": issue.line_number or 0,
                        "confidence":  issue.confidence,
                        "file_path":   state.file_path,
                        "repo_url":    state.repo_url,
                        "session_id":  review_id,
                        "now":         datetime.now().isoformat(),
                    },
                )
            written += 1
        return written

    def _detect_and_promote_patterns(self, repo_url: str) -> int:
        """Find cross-file issue patterns and promote them to Pattern nodes.

        A pattern is detected when the same ``category + severity`` combination
        appears across at least ``PATTERN_THRESHOLD`` distinct files in the repo.

        - New pattern  → create Pattern node, link to all affected File nodes.
        - Existing pattern → update ``affected_files`` and ``last_seen``.

        Args:
            repo_url: Repository URL used to scope the search.

        Returns:
            Number of *new* Pattern nodes created in this call.
        """
        candidates = self.client.query(
            """
            MATCH (i:Issue {repo_url: $repo_url})
            WITH i.category AS category,
                 i.severity  AS severity,
                 collect(DISTINCT i.file_path) AS files,
                 count(DISTINCT i.file_path)   AS file_count
            WHERE file_count >= $threshold
            RETURN category, severity, files, file_count
            """,
            {"repo_url": repo_url, "threshold": self.PATTERN_THRESHOLD},
        )

        new_patterns = 0
        for row in candidates:
            pattern_id = f"{repo_url}:{row['category']}:{row['severity']}"
            description = (
                f"{row['severity']} {row['category']} issues "
                f"across {row['file_count']} files"
            )

            existing = self.client.query_single(
                "MATCH (p:Pattern {pattern_id: $pid}) RETURN p.pattern_id",
                {"pid": pattern_id},
            )

            if not existing:
                # Create new Pattern and AFFECTS edges to every impacted file
                self.client.query(
                    """
                    CREATE (p:Pattern {
                        pattern_id:       $pattern_id,
                        description:      $description,
                        category:         $category,
                        severity:         $severity,
                        affected_files:   $count,
                        first_seen:       $now,
                        last_seen:        $now,
                        occurrence_count: $count
                    })
                    WITH p
                    UNWIND $files AS file_path
                    MATCH (f:File {path: file_path, repo_url: $repo_url})
                    CREATE (p)-[:AFFECTS]->(f)
                    """,
                    {
                        "pattern_id":  pattern_id,
                        "description": description,
                        "category":    row["category"],
                        "severity":    row["severity"],
                        "files":       row["files"],
                        "count":       row["file_count"],
                        "repo_url":    repo_url,
                        "now":         datetime.now().isoformat(),
                    },
                )
                new_patterns += 1
                logger.info(f"New pattern promoted: {description}")
            else:
                # Keep existing pattern stats fresh
                self.client.query(
                    """
                    MATCH (p:Pattern {pattern_id: $pattern_id})
                    SET p.affected_files   = $count,
                        p.last_seen        = $now,
                        p.occurrence_count = $count
                    """,
                    {
                        "pattern_id": pattern_id,
                        "count":      row["file_count"],
                        "now":        datetime.now().isoformat(),
                    },
                )

        return new_patterns