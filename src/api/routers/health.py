"""
Health Router
=============
GET /health  — API + dependency health check
GET /        — API root info
"""

from fastapi import APIRouter, Depends
from src.api.models import HealthResponse
from src.api.dependencies import AppState, get_app_state

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
async def health_check(
    state: AppState = Depends(get_app_state),
) -> HealthResponse:
    """Return current API health status.

    - ``healthy``   — Groq connected AND Neo4j connected
    - ``degraded``  — Groq connected, Neo4j unavailable (reviews work, no memory)
    - ``unhealthy`` — Groq not connected (no reviews possible)
    """
    if not state.groq_connected:
        status = "unhealthy"
    elif not state.neo4j_connected:
        status = "degraded"
    else:
        status = "healthy"

    return HealthResponse(
        status=status,
        neo4j_connected=state.neo4j_connected,
        groq_connected=state.groq_connected,
        uptime_seconds=round(state.uptime_seconds, 2),
        total_reviews_this_session=state.review_count,
    )


@router.get("/")
async def root() -> dict:
    """API root — links to docs and key endpoints."""
    return {
        "name":    "Code Review Agent API",
        "version": "1.0.0",
        "docs":    "/docs",
        "health":  "/health",
        "ui":      "/app",
    }