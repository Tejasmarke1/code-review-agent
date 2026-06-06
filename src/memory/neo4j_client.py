"""
Neo4j Client
=============
"""

import sys
from pathlib import Path
from typing import Optional

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import NEO4J
from src.memory.graph_schema import SCHEMA_QUERIES


class Neo4jClient:
    """
    Thread-safe Neo4j client with connection pooling and auto-reconnect.
    Singleton — one instance per application.
    """

    _instance: Optional["Neo4jClient"] = None

    def __init__(self) -> None:
        self._driver    = None
        self._connected = False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @classmethod
    def get_instance(cls) -> "Neo4jClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def connect(self) -> bool:
        """Establish (or re-establish) connection to Neo4j.

        Returns:
            True on success, False on any failure (never raises).
        """
        try:
            from neo4j import GraphDatabase
        except ImportError:
            logger.error("neo4j package not installed. Run: pip install neo4j")
            return False

        try:
            if self._driver:
                try:
                    self._driver.close()
                except Exception:
                    pass
                self._driver = None

            self._driver = GraphDatabase.driver(
                NEO4J["uri"],
                auth=(NEO4J["username"], NEO4J["password"]),
                max_connection_lifetime=300,
                connection_acquisition_timeout=60,
                max_connection_pool_size=10,
            )
            self._driver.verify_connectivity()
            self._connected = True
            logger.success(f"Connected to Neo4j at {NEO4J['uri']}")
            return True

        except Exception as e:
            err = str(e)
            if "AuthError" in type(e).__name__ or "Unauthorized" in err:
                logger.error(
                    "Neo4j auth failed. Check NEO4J_USERNAME / NEO4J_PASSWORD in .env"
                )
            elif "ServiceUnavailable" in type(e).__name__ or "refused" in err.lower():
                logger.error(
                    f"Neo4j not reachable at {NEO4J['uri']}. "
                    "Start Neo4j or check credentials."
                )
            else:
                logger.error(f"Neo4j connection failed: {e}")
            self._driver    = None
            self._connected = False
            return False

    def initialize_schema(self) -> None:
        """Create constraints and indexes (idempotent via IF NOT EXISTS)."""
        if not self._connected:
            logger.warning("Cannot initialize schema — not connected")
            return
        for query in SCHEMA_QUERIES:
            try:
                self.query(query, {})
            except Exception as e:
                logger.debug(f"Schema note (usually harmless): {e}")
        logger.success("Neo4j schema initialized")

    def query(self, cypher: str, params: dict) -> list[dict]:
        """Execute a Cypher query with automatic reconnect on stale connections.

        Args:
            cypher: Cypher string with $param placeholders.
            params: Parameter dict — values bound safely, never interpolated.

        Returns:
            List of result records as plain dicts.

        Raises:
            RuntimeError: If not connected and reconnect also fails.
        """
        if not self._connected or self._driver is None:
            raise RuntimeError("Neo4j not connected. Call connect() first.")

        database = NEO4J.get("database") or None

        for attempt in range(2):
            session = None
            try:
                session = self._driver.session(database=database)
                result  = session.run(cypher, params)
                return [dict(record) for record in result]

            except Exception as e:
                err_name = type(e).__name__
                is_stale = any(
                    kw in err_name or kw in str(e)
                    for kw in ("ServiceUnavailable", "SessionExpired", "defunct",
                               "ConnectionResetError", "BrokenPipe")
                )
                if is_stale and attempt == 0:
                    logger.warning(
                        f"Neo4j connection stale, reconnecting... ({err_name})"
                    )
                    try:
                        self._driver.close()
                    except Exception:
                        pass
                    self._connected = False
                    self.connect()
                    continue
                raise

            finally:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass

        raise RuntimeError("Neo4j query failed after reconnect attempt")

    def query_single(self, cypher: str, params: dict) -> Optional[dict]:
        """Execute a query and return the first result, or None."""
        results = self.query(cypher, params)
        return results[0] if results else None

    def close(self) -> None:
        """Close the driver connection pool."""
        if self._driver:
            try:
                self._driver.close()
            except Exception as e:
                logger.debug(f"Neo4j driver close note: {e}")
            self._driver = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected