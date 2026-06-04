"""
Memory Router
=============
GET    /memory/stats              — Neo4j graph statistics
GET    /memory/file               — history for a specific file
GET    /memory/patterns           — all detected patterns for a repo
DELETE /memory/repo               — clear all memory for a repo (testing only)
"""

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import AppState, get_app_state
from src.api.models import (
    FileMemoryResponse,
    MemoryStatsResponse,
    PatternResponse,
)
from src.memory.neo4j_client import Neo4jClient

router = APIRouter(prefix="/memory", tags=["Memory"])


def _require_neo4j() -> Neo4jClient:
    """Return the singleton Neo4jClient or raise 503 if not connected.

    Returns:
        Connected Neo4jClient instance.

    Raises:
        HTTPException 503: If Neo4j is not connected.
    """
    client = Neo4jClient.get_instance()
    if not client.is_connected:
        raise HTTPException(
            status_code=503,
            detail=(
                "Neo4j not connected. "
                "Set NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD in .env and restart."
            ),
        )
    return client


@router.get("/stats", response_model=MemoryStatsResponse)
async def get_memory_stats(
    state: AppState = Depends(get_app_state),
) -> MemoryStatsResponse:
    """Return Neo4j graph-wide statistics.

    Returns ``connected=False`` with zero counts when Neo4j is unavailable
    rather than raising — lets the UI show a degraded-mode banner gracefully.
    """
    client = Neo4jClient.get_instance()

    if not client.is_connected:
        return MemoryStatsResponse(
            connected=False,
            node_counts={},
            total_relationships=0,
            total_reviews=0,
            total_issues=0,
            total_patterns=0,
        )

    try:
        node_results = client.query(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS count",
            {},
        )
        node_counts = {
            r["label"]: r["count"]
            for r in node_results
            if r["label"]
        }

        rel_result = client.query_single(
            "MATCH ()-[r]->() RETURN count(r) AS total", {}
        )
        total_rel = rel_result["total"] if rel_result else 0

        return MemoryStatsResponse(
            connected=True,
            node_counts=node_counts,
            total_relationships=total_rel,
            total_reviews=node_counts.get("Review", 0),
            total_issues=node_counts.get("Issue", 0),
            total_patterns=node_counts.get("Pattern", 0),
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Memory stats query failed: {str(e)[:200]}",
        )


@router.get("/file", response_model=FileMemoryResponse)
async def get_file_memory(
    file_path: str = Query(..., description="Absolute file path to look up"),
    repo_url: str = Query(..., description="Repository URL"),
) -> FileMemoryResponse:
    """Return full memory history for a specific file.

    Args:
        file_path: Absolute path of the file (as stored in Neo4j).
        repo_url: Repository URL used to scope the lookup.

    Returns:
        FileMemoryResponse with review history and all past issues.

    Raises:
        404: File has not been reviewed yet.
        503: Neo4j not connected.
    """
    client = _require_neo4j()

    file_result = client.query_single(
        """
        MATCH (f:File {path: $path, repo_url: $repo_url})
        RETURN f.review_count AS review_count,
               f.avg_risk_score AS avg_risk
        """,
        {"path": file_path, "repo_url": repo_url},
    )

    if not file_result:
        raise HTTPException(
            status_code=404,
            detail=f"File not found in memory: {file_path}. Review it first.",
        )

    try:
        issues = client.query(
            """
            MATCH (f:File {path: $path, repo_url: $repo_url})-[:HAS_ISSUE]->(i:Issue)
            RETURN i.title           AS title,
                   i.severity        AS severity,
                   i.category        AS category,
                   i.occurrence_count AS count,
                   i.source_tool     AS source_tool,
                   i.line_number     AS line_number
            ORDER BY i.occurrence_count DESC
            """,
            {"path": file_path, "repo_url": repo_url},
        )

        reviews = client.query(
            """
            MATCH (f:File {path: $path, repo_url: $repo_url})-[:HAS_REVIEW]->(r:Review)
            RETURN r.session_id   AS session_id,
                   r.timestamp    AS timestamp,
                   r.issues_found AS issues_found,
                   r.risk_score   AS risk_score,
                   r.status       AS status
            ORDER BY r.timestamp DESC
            LIMIT 5
            """,
            {"path": file_path, "repo_url": repo_url},
        )

        return FileMemoryResponse(
            file_path=file_path,
            repo_url=repo_url,
            review_count=file_result.get("review_count", 0) or 0,
            avg_risk_score=file_result.get("avg_risk", 0.0) or 0.0,
            past_issues=issues,
            past_reviews=reviews,
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"File memory query failed: {str(e)[:200]}",
        )


@router.get("/patterns", response_model=PatternResponse)
async def get_patterns(
    repo_url: str = Query(..., description="Repository URL to get patterns for"),
) -> PatternResponse:
    """Return all cross-file patterns detected for a repository.

    Args:
        repo_url: Repository URL to scope the pattern lookup.

    Returns:
        PatternResponse with all patterns and total count.

    Raises:
        503: Neo4j not connected.
    """
    client = _require_neo4j()

    try:
        patterns = client.query(
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
        return PatternResponse(patterns=patterns, total=len(patterns))

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Patterns query failed: {str(e)[:200]}",
        )


@router.delete("/repo")
async def clear_repo_memory(
    repo_url: str = Query(..., description="Repository URL to clear"),
) -> dict:
    """Delete ALL graph data for a repository.

    **Use with caution** — permanently removes Repo, File, Review, Issue,
    and Pattern nodes for the given repo URL.  Intended for testing and
    cleanup only.

    Args:
        repo_url: The repository URL whose data should be deleted.

    Returns:
        Confirmation dict with ``deleted=True`` and the repo_url.

    Raises:
        503: Neo4j not connected.
        500: Delete query failed.
    """
    client = _require_neo4j()

    try:
        client.query(
            """
            MATCH (r:Repo {url: $repo_url})
            OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
            OPTIONAL MATCH (f)-[:HAS_REVIEW]->(rv:Review)
            OPTIONAL MATCH (f)-[:HAS_ISSUE]->(i:Issue)
            OPTIONAL MATCH (p:Pattern)-[:AFFECTS]->(f)
            DETACH DELETE r, f, rv, i, p
            """,
            {"repo_url": repo_url},
        )
        return {"deleted": True, "repo_url": repo_url}

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Delete failed: {str(e)[:200]}",
        )