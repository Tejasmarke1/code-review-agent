"""
Streaming Review Router — SSE endpoint for live agent trace.
"""
import asyncio
import json
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from loguru import logger

from src.api.dependencies import AppState, get_app_state

router = APIRouter(prefix="/stream", tags=["Streaming"])


def sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"


class StreamingReviewRunner:
    def __init__(self, app_state: AppState) -> None:
        self.app_state = app_state

    async def run(self, repo_url: str, files: list[str],
                  max_files: int, use_memory: bool) -> AsyncGenerator[str, None]:
        loop  = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        session_id = str(uuid.uuid4())[:8]

        yield sse("status", {
            "type": "session_started", "session_id": session_id,
            "repo_url": repo_url, "files_count": len(files), "timestamp": time.time(),
        })

        def _push(event_type: str, data: dict) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (event_type, data))

        def on_step(d):          _push("step",   d)
        def on_issue(d):         _push("issue",  d)
        def on_file_complete(d): _push("status", {"type": "file_complete", **d})

        orch = self.app_state.orchestrator
        if orch is None:
            yield sse("error", {"message": "Orchestrator not initialised"})
            yield sse("done",  {"error":   "Orchestrator not initialised"})
            return

        orch._on_step_callback          = on_step
        orch._on_issue_callback         = on_issue
        orch._on_file_complete_callback = on_file_complete

        async def _run_blocking():
            try:
                session = await loop.run_in_executor(
                    None,
                    lambda: orch.review_repo(
                        repo_url=repo_url,
                        files_to_review=files if files else None,
                        max_files=max_files,
                        use_defect_api=False,
                    ),
                )
                _push("done", {
                    "session_id":      session.session_id,
                    "files_reviewed":  len(session.files_reviewed),
                    "total_issues":    session.total_issues,
                    "critical_issues": session.critical_issues,
                    "high_issues":     session.high_issues,
                    "patterns":        session.patterns_detected,
                    "elapsed_seconds": session.total_elapsed_seconds,
                    "errors":          session.errors,
                })
            except Exception as e:
                logger.error(f"Streaming review error: {e}")
                _push("error", {"message": str(e)[:300]})
                _push("done",  {"error":   str(e)[:300]})
            finally:
                orch._on_step_callback          = None
                orch._on_issue_callback         = None
                orch._on_file_complete_callback = None

        asyncio.create_task(_run_blocking())

        while True:
            try:
                event_type, data = await asyncio.wait_for(queue.get(), timeout=360)
                yield sse(event_type, data)
                if event_type == "done":
                    break
            except asyncio.TimeoutError:
                yield sse("error", {"message": "Review timed out after 6 minutes"})
                yield sse("done",  {"error":   "timeout"})
                break
            except Exception as e:
                yield sse("error", {"message": str(e)})
                yield sse("done",  {"error":   str(e)})
                break


@router.get("/review")
async def stream_review(
    repo_url:   str  = Query(..., description="Repository URL to review"),
    files:      str  = Query("",  description="Comma-separated absolute file paths"),
    max_files:  int  = Query(3,   description="Max files to review"),
    use_memory: bool = Query(True),
    state: AppState  = Depends(get_app_state),
) -> StreamingResponse:
    """SSE endpoint. Connect with EventSource('/stream/review?repo_url=...')."""
    if not state.groq_connected or state.orchestrator is None:
        async def _err():
            yield sse("error", {"message": "Groq LLM not available"})
            yield sse("done",  {"error":   "service unavailable"})
        return StreamingResponse(_err(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    file_list = [f.strip() for f in files.split(",") if f.strip()] if files else []
    runner    = StreamingReviewRunner(state)
    return StreamingResponse(
        runner.run(repo_url, file_list, min(max_files, 10), use_memory),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )