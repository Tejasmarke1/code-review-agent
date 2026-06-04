"""
Memory Tools — Neo4j Query Tools for the Agent
===============================================
These tools let the agent actively query its own memory during a review.
The agent can ask: "have I seen this issue before?" or
"what patterns exist in this repo?"

These are registered in the ToolRegistry so the agent can call them
just like ruff or bandit — as part of the ReAct loop.

Registration is skipped silently when Neo4j is unavailable so the
rest of the tool set continues to work normally.
"""

import sys
from pathlib import Path
from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.tools.registry import Tool, ToolRegistry
from src.memory.neo4j_client import Neo4jClient
from src.memory.memory_retriever import MemoryRetriever


def search_past_issues(
    repo_url: str,
    category: str = "",
    severity: str = "",
) -> str:
    """Search Neo4j memory for past issues in this repo matching optional filters.

    The agent calls this when it suspects a recurring problem — for example,
    "have we seen SQL injection in this repo before?"

    Args:
        repo_url: Repository URL to scope the search.
        category: Optional filter — security / style / complexity / logic / performance.
        severity: Optional filter — LOW / MEDIUM / HIGH / CRITICAL.

    Returns:
        Formatted string listing matching past issues, or a not-found message.
    """
    client = Neo4jClient.get_instance()
    if not client.is_connected:
        return "Memory not available — Neo4j not connected."

    params: dict = {"repo_url": repo_url}
    where_clauses = ["i.repo_url = $repo_url"]

    if category:
        where_clauses.append("i.category = $category")
        params["category"] = category.lower()
    if severity:
        where_clauses.append("i.severity = $severity")
        params["severity"] = severity.upper()

    where = " AND ".join(where_clauses)

    try:
        results = client.query(
            f"""
            MATCH (i:Issue)
            WHERE {where}
            RETURN i.title           AS title,
                   i.file_path       AS file_path,
                   i.severity        AS severity,
                   i.category        AS category,
                   i.occurrence_count AS count,
                   i.last_seen       AS last_seen
            ORDER BY i.occurrence_count DESC
            LIMIT 10
            """,
            params,
        )
    except Exception as e:
        return f"Memory query failed: {e}"

    if not results:
        filters = (
            f" (category={category}, severity={severity})"
            if category or severity
            else ""
        )
        return f"No past issues found in this repo{filters}."

    lines = [f"Past issues in repo ({len(results)} found):"]
    for r in results:
        last_seen = (r["last_seen"] or "")[:10] if r.get("last_seen") else "unknown"
        lines.append(
            f"  [{r['severity']}] {r['title']} "
            f"in {Path(r['file_path']).name} "
            f"(seen {r['count']}x, last: {last_seen})"
        )
    return "\n".join(lines)


def get_file_review_history(file_path: str, repo_url: str) -> str:
    """Get the full review history for a specific file.

    Shows review dates, issue counts, and risk score trend across past sessions.

    Args:
        file_path: Path to the file to query.
        repo_url: Repository URL used to scope the lookup.

    Returns:
        Formatted review history string, or a first-review message if none exists.
    """
    client = Neo4jClient.get_instance()
    if not client.is_connected:
        return "Memory not available — Neo4j not connected."

    try:
        results = client.query(
            """
            MATCH (f:File {path: $file_path, repo_url: $repo_url})-[:HAS_REVIEW]->(rv:Review)
            RETURN rv.session_id     AS session_id,
                   rv.timestamp      AS timestamp,
                   rv.risk_score     AS risk_score,
                   rv.issues_found   AS issues_found,
                   rv.status         AS status,
                   rv.steps_taken    AS steps
            ORDER BY rv.timestamp DESC
            LIMIT 5
            """,
            {"file_path": file_path, "repo_url": repo_url},
        )
    except Exception as e:
        return f"Memory query failed: {e}"

    if not results:
        return (
            f"No review history found for {Path(file_path).name}. "
            "This is the first review."
        )

    lines = [
        f"Review history for {Path(file_path).name} ({len(results)} reviews):"
    ]
    for r in results:
        date = (r["timestamp"] or "")[:10] if r.get("timestamp") else "unknown"
        lines.append(
            f"  {date}: {r['issues_found']} issues found, "
            f"risk={r['risk_score']:.2f}, "
            f"status={r['status']}, "
            f"steps={r['steps']}"
        )
    return "\n".join(lines)


def get_repo_patterns(repo_url: str) -> str:
    """Get all systemic patterns detected across a repository.

    Patterns are cross-file issues that appear in 3 or more files,
    promoting them to first-class graph nodes for tracking.

    Args:
        repo_url: Repository URL to query patterns for.

    Returns:
        Formatted pattern list, or a guidance message if none exist yet.
    """
    client = Neo4jClient.get_instance()
    if not client.is_connected:
        return "Memory not available — Neo4j not connected."

    try:
        results = client.query(
            """
            MATCH (p:Pattern)-[:AFFECTS]->(f:File {repo_url: $repo_url})
            WITH p, count(f) AS affected_files
            RETURN p.description      AS description,
                   p.category         AS category,
                   p.severity         AS severity,
                   p.occurrence_count AS occurrences,
                   affected_files
            ORDER BY affected_files DESC
            """,
            {"repo_url": repo_url},
        )
    except Exception as e:
        return f"Memory query failed: {e}"

    if not results:
        return (
            "No patterns detected yet. Patterns emerge after reviewing "
            "3+ files with similar issues."
        )

    lines = [
        f"Detected patterns in this repository ({len(results)} patterns):"
    ]
    for r in results:
        lines.append(
            f"  [{r['severity']}] {r['description']} "
            f"— affects {r['affected_files']} files"
        )
    return "\n".join(lines)


def register_memory_tools(registry: ToolRegistry) -> None:
    """Register all memory query tools into the given agent tool registry.

    Registration is skipped silently when Neo4j is not connected, so the
    rest of the tool set continues to work in memory-less mode.

    Args:
        registry: The ToolRegistry instance to register tools into.
    """
    client = Neo4jClient.get_instance()
    if not client.is_connected:
        logger.debug(
            "Neo4j not connected — skipping memory tool registration"
        )
        return

    registry.register(Tool(
        name="search_past_issues",
        description=(
            "Search Neo4j memory for past issues in this repo. "
            "Use when you suspect a recurring problem — e.g. "
            "'have we seen SQL injection here before?'"
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo_url": {
                    "type": "string",
                    "description": "Repository URL to search",
                },
                "category": {
                    "type": "string",
                    "description": "Optional: security/style/complexity/logic/performance",
                },
                "severity": {
                    "type": "string",
                    "description": "Optional: LOW/MEDIUM/HIGH/CRITICAL",
                },
            },
            "required": ["repo_url"],
        },
        func=search_past_issues,
        category="memory",
    ))

    registry.register(Tool(
        name="get_file_review_history",
        description=(
            "Get the review history for a specific file. "
            "Shows past risk scores and issue counts. "
            "Use at the start of reviewing a file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Path to the file",
                },
                "repo_url": {
                    "type": "string",
                    "description": "Repository URL",
                },
            },
            "required": ["file_path", "repo_url"],
        },
        func=get_file_review_history,
        category="memory",
    ))

    registry.register(Tool(
        name="get_repo_patterns",
        description=(
            "Get all systemic patterns detected across this repository. "
            "Use at the start of a repo review session to understand known problems."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo_url": {
                    "type": "string",
                    "description": "Repository URL",
                },
            },
            "required": ["repo_url"],
        },
        func=get_repo_patterns,
        category="memory",
    ))

    logger.debug("Memory tools registered: search_past_issues, get_file_review_history, get_repo_patterns")