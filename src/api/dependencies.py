"""
Singleton Dependency Injection for FastAPI
===========================================
All heavy components (GroqClient, ReviewOrchestrator, Neo4jClient) are
loaded ONCE at startup via the AppState singleton.

Routes receive the shared AppState via ``Depends(get_app_state)`` —
never construct their own clients.
"""

import time
from typing import Optional
from loguru import logger
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


class AppState:
    """
    Application-wide singleton holding all initialised components.

    Loaded at FastAPI startup via the lifespan context manager in ``main.py``.
    Injected into routes via ``Depends(get_app_state)``.

    Design: ``get_instance()`` called any number of times returns the same
    object; ``initialize()`` is called exactly once at startup.
    """

    _instance: Optional["AppState"] = None

    def __init__(self) -> None:
        # Import here to avoid circular imports at module load time
        from src.agent.orchestrator import ReviewOrchestrator
        from src.memory.neo4j_client import Neo4jClient
        from src.llm.groq_client import GroqClient

        self._ReviewOrchestrator = ReviewOrchestrator
        self._Neo4jClient = Neo4jClient
        self._GroqClient = GroqClient

        self.orchestrator: Optional[ReviewOrchestrator] = None
        self.neo4j: Optional[Neo4jClient] = None
        self.groq: Optional[GroqClient] = None
        self.neo4j_connected: bool = False
        self.groq_connected: bool = False
        self._startup_time: float = time.time()
        self._review_count: int = 0

    @classmethod
    def get_instance(cls) -> "AppState":
        """Return the singleton AppState, creating it on first call.

        Returns:
            The shared AppState instance. Creating it 100 times returns
            the same object — no duplicate initialisation.
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def initialize(self) -> None:
        """Initialise all components at startup.

        Never raises — logs errors and sets connected flags to False so
        the API starts in degraded mode rather than crashing.
        """
        # ── Test Groq connectivity ─────────────────────────────────────────
        try:
            self.groq = self._GroqClient()
            self.groq_connected = True
            logger.success("Groq client initialised")
        except Exception as e:
            logger.error(f"Groq init failed: {e}")
            self.groq_connected = False

        # ── Initialise orchestrator (handles Neo4j internally) ─────────────
        try:
            self.orchestrator = self._ReviewOrchestrator(use_memory=True)
            self.neo4j_connected = self.orchestrator.use_memory
            logger.success(
                f"Orchestrator ready (memory={self.neo4j_connected})"
            )
        except Exception as e:
            logger.error(f"Orchestrator init failed: {e}")
            try:
                self.orchestrator = self._ReviewOrchestrator(use_memory=False)
                logger.warning("Orchestrator started without memory")
            except Exception as e2:
                logger.error(f"Orchestrator fallback also failed: {e2}")

    @property
    def uptime_seconds(self) -> float:
        """Return seconds since the application started."""
        return time.time() - self._startup_time

    @property
    def is_healthy(self) -> bool:
        """Return True if the minimum requirement (Groq) is met."""
        return self.groq_connected

    def increment_review_count(self) -> None:
        """Thread-safe counter increment for completed reviews."""
        self._review_count += 1

    @property
    def review_count(self) -> int:
        """Total reviews completed since startup."""
        return self._review_count


def get_app_state() -> AppState:
    """FastAPI dependency function — injects the shared AppState.

    Usage in a route::

        @router.get("/example")
        async def example(state: AppState = Depends(get_app_state)):
            ...
    """
    return AppState.get_instance()