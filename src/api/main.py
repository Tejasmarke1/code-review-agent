"""
FastAPI Application — Code Review Agent
=========================================
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from src.api.dependencies import AppState
from src.api.routers import health, memory, review, stream


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Code Review Agent API...")
    state = AppState.get_instance()
    state.initialize()

    if state.is_healthy:
        logger.success(
            f"API ready — groq={state.groq_connected} neo4j={state.neo4j_connected}"
        )
    else:
        logger.warning("API starting in degraded mode — check GROQ_API_KEY in .env")

    yield  # server runs here

    logger.info("Shutting down API...")

    try:
        from src.tools.github_tools import cleanup_all_clones
        cleanup_all_clones()
    except Exception as e:
        logger.debug(f"GitHub cleanup note: {e}")

    from src.memory.neo4j_client import Neo4jClient
    client = Neo4jClient.get_instance()
    if client.is_connected:
        client.close()
        logger.info("Neo4j connection closed cleanly")


app = FastAPI(
    title="Code Review Agent",
    description=(
        "Autonomous AI code review agent with Neo4j graph memory, "
        "real-time SSE streaming, and GitHub integration."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(review.router)
app.include_router(memory.router)
app.include_router(stream.router)

_ui_dir = Path(__file__).parent.parent.parent / "ui"
if _ui_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_ui_dir)), name="ui")
    logger.info(f"UI static files mounted from {_ui_dir}")

    @app.get("/app", include_in_schema=False)
    async def serve_ui() -> FileResponse:
        return FileResponse(str(_ui_dir / "index.html"))
else:
    logger.warning(f"UI directory not found: {_ui_dir}")
