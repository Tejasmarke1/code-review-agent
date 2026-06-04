"""
Review Router
=============
POST /review/          — start a review session (blocking, returns when done)
GET  /review/{id}      — get a completed review by session ID
GET  /review/list      — list recent in-memory reviews
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from src.api.dependencies import AppState, get_app_state
from src.api.models import (
    FileReviewResult,
    IssueResult,
    ReviewRequest,
    ReviewResponse,
)
from src.agent.orchestrator import OrchestratorSession

router = APIRouter(prefix="/review", tags=["Review"])

# In-memory session store — keyed by session_id
# Replace with Redis/DB persistence on Day 4 if needed
_completed_reviews: dict[str, ReviewResponse] = {}

# Thread pool for running the blocking orchestrator without stalling the event loop
_executor = ThreadPoolExecutor(max_workers=2)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _session_to_response(session: OrchestratorSession) -> ReviewResponse:
    """Convert an OrchestratorSession dataclass to a ReviewResponse Pydantic model.

    Args:
        session: Completed OrchestratorSession from ReviewOrchestrator.review_repo().

    Returns:
        Fully populated ReviewResponse ready for JSON serialisation.
    """
    medium_count = sum(
        1 for s in session.file_states
        for i in s.issues_found
        if i.severity.value == "MEDIUM"
    )
    low_count = sum(
        1 for s in session.file_states
        for i in s.issues_found
        if i.severity.value == "LOW"
    )

    file_results: list[FileReviewResult] = []
    for state in session.file_states:
        issues = [
            IssueResult(
                title=i.title,
                severity=i.severity.value,
                category=i.category,
                line_number=i.line_number,
                description=i.description,
                suggestion=i.suggestion,
                source_tool=i.source_tool,
                confidence=i.confidence,
            )
            for i in state.issues_found
        ]
        file_results.append(
            FileReviewResult(
                file_path=state.file_path,
                status=state.status.value,
                risk_score=state.risk_score,
                risk_label=state.risk_label,
                steps_taken=state.current_step,
                elapsed_seconds=state.elapsed_seconds,
                issues=issues,
                final_review=state.final_review or "",
                summary=state.summary or "",
            )
        )

    return ReviewResponse(
        session_id=session.session_id,
        repo_url=session.repo_url,
        started_at=session.started_at.isoformat(),
        completed_at=(
            session.completed_at.isoformat() if session.completed_at else None
        ),
        files_reviewed=len(session.files_reviewed),
        total_issues=session.total_issues,
        critical_issues=session.critical_issues,
        high_issues=session.high_issues,
        medium_issues=medium_count,
        low_issues=low_count,
        patterns_detected=session.patterns_detected,
        total_elapsed_seconds=session.total_elapsed_seconds,
        file_results=file_results,
        repo_summary=session.repo_summary,
        errors=session.errors,
    )


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/", response_model=ReviewResponse)
async def start_review(
    request: ReviewRequest,
    state: AppState = Depends(get_app_state),
) -> ReviewResponse:
    """Start a code review session and return full results when complete.

    The orchestrator runs in a ``ThreadPoolExecutor`` so the async event loop
    is never blocked by LLM calls or tool execution.

    For large repos or slow Groq responses, clients should set a long
    HTTP timeout (300+ seconds).

    Args:
        request: ReviewRequest body — repo_url, optional files list, options.
        state: Injected AppState singleton.

    Returns:
        ReviewResponse with all file results, issues, and the repo summary.

    Raises:
        503: Groq or orchestrator not available.
        500: Unexpected error during review.
    """
    if not state.groq_connected:
        raise HTTPException(
            status_code=503,
            detail="Groq LLM not available. Check GROQ_API_KEY in .env",
        )
    if state.orchestrator is None:
        raise HTTPException(
            status_code=503,
            detail="Orchestrator not initialised. Check server logs.",
        )

    logger.info(f"API: Review requested — {request.repo_url}")

    try:
        loop = asyncio.get_event_loop()
        session = await loop.run_in_executor(
            _executor,
            lambda: state.orchestrator.review_repo(
                repo_url=request.repo_url,
                files_to_review=request.files,
                use_defect_api=request.use_defect_api,
                max_files=request.max_files,
            ),
        )

        response = _session_to_response(session)
        _completed_reviews[session.session_id] = response
        state.increment_review_count()

        logger.success(
            f"API: Review complete — session={session.session_id} "
            f"files={response.files_reviewed} issues={response.total_issues}"
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Review failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Review failed: {str(e)[:300]}",
        )


@router.get("/list", response_model=list[ReviewResponse])
async def list_reviews(limit: int = 10) -> list[ReviewResponse]:
    """List the most recent completed reviews (in-memory only).

    Args:
        limit: Maximum number of reviews to return (default 10).

    Returns:
        List of ReviewResponse objects, most recent last.
    """
    reviews = list(_completed_reviews.values())
    return reviews[-limit:]


@router.get("/{session_id}", response_model=ReviewResponse)
async def get_review(session_id: str) -> ReviewResponse:
    """Retrieve a previously completed review by session ID.

    Args:
        session_id: The session ID returned by POST /review/.

    Returns:
        ReviewResponse for the requested session.

    Raises:
        404: Session not found (may be on a different server instance).
    """
    if session_id not in _completed_reviews:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Review session '{session_id}' not found. "
                "Only sessions from this server instance are stored in memory."
            ),
        )
    return _completed_reviews[session_id]