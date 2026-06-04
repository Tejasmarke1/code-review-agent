"""
FastAPI Application — Code Review Agent
=========================================
Startup:  initialise AppState singleton (loads GroqClient + Neo4j)
Shutdown: log clean exit
Routes:   /health, /review, /memory
UI:       served from /ui/* (StaticFiles) and /app (index.html)
CORS:     open for local UI development
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from src.api.dependencies import AppState
from src.api.routers import health, memory, review


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise heavy components on startup; clean up on shutdown."""
    logger.info("Starting Code Review Agent API...")
    state = AppState.get_instance()
    state.initialize()

    if state.is_healthy:
        logger.success(
            "API ready — "
            f"groq={state.groq_connected} neo4j={state.neo4j_connected}"
        )
    else:
        logger.warning(
            "API starting in degraded mode. "
            "Check GROQ_API_KEY in .env"
        )

    yield  # ← server is running here

    logger.info("Shutting down API...")
    # Ensure Neo4j driver closes cleanly (Belt-and-suspenders — __del__ also calls close)
    from src.memory.neo4j_client import Neo4jClient
    client = Neo4jClient.get_instance()
    if client.is_connected:
        client.close()
        logger.info("Neo4j connection closed cleanly")


app = FastAPI(
    title="Code Review Agent",
    description=(
        "Autonomous AI agent for code review with Neo4j graph memory "
        "and Defect Prediction Engine integration."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ───────────────────────────────────────────────────────────────────────
# Open for local UI / notebook development.
# Restrict ``allow_origins`` in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Routers ────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(review.router)
app.include_router(memory.router)

# ── Static UI ──────────────────────────────────────────────────────────────────
_ui_dir = Path(__file__).parent.parent.parent / "ui"

if _ui_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_ui_dir)), name="ui")
    logger.info(f"UI static files mounted from {_ui_dir}")

    @app.get("/app", include_in_schema=False)
    async def serve_ui() -> FileResponse:
        """Serve the single-page UI."""
        return FileResponse(str(_ui_dir / "index.html"))
else:
    logger.warning(f"UI directory not found: {_ui_dir}")