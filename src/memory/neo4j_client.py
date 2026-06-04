"""
Neo4j Client
=============
Low-level connection and query execution.
All other memory files use this — never import neo4j driver directly elsewhere.

Uses Neo4j AuraDB free tier or local instance.
Connection string from configs/config.py NEO4J section.
Supports both bolt:// (local) and neo4j+s:// (AuraDB) URI schemes.
"""

import sys
from pathlib import Path
from typing import Any, Optional

from loguru import logger

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import NEO4J
from src.memory.graph_schema import SCHEMA_QUERIES


class Neo4jClient:
    """
    Thread-safe Neo4j client with connection pooling.
    Singleton — one instance per application.

    Usage::

        client = Neo4jClient.get_instance()
        client.connect()
        results = client.query("MATCH (r:Repo) RETURN r.url", {})
        client.close()

    Supports both local (bolt://) and AuraDB (neo4j+s://) connections.
    """

    _instance: Optional["Neo4jClient"] = None

    def __init__(self) -> None:
        self._driver = None
        self._connected = False

    @classmethod
    def get_instance(cls) -> "Neo4jClient":
        """Return the singleton Neo4jClient, creating it on first call.

        Returns:
            The shared Neo4jClient instance.
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def connect(self) -> bool:
        """Establish connection to Neo4j.

        Handles both ``bolt://`` (local) and ``neo4j+s://`` (AuraDB) URI schemes.

        Returns:
            True if connection succeeded, False otherwise. Never raises —
            all errors are logged with actionable troubleshooting hints.
        """
        try:
            from neo4j import GraphDatabase, exceptions as neo4j_exc
        except ImportError:
            logger.error(
                "neo4j package not installed. Run: pip install neo4j"
            )
            return False

        try:
            self._driver = GraphDatabase.driver(
                NEO4J["uri"],
                auth=(NEO4J["username"], NEO4J["password"]),
            )
            # Verify connectivity with a lightweight round-trip
            self._driver.verify_connectivity()
            self._connected = True
            logger.success(f"Connected to Neo4j at {NEO4J['uri']}")
            return True

        except Exception as e:
            err = str(e)
            if "AuthError" in type(e).__name__ or "Unauthorized" in err:
                logger.error(
                    "Neo4j authentication failed. "
                    "Check NEO4J_USERNAME and NEO4J_PASSWORD in .env\n"
                    "For AuraDB: password is in the downloaded credentials file."
                )
            elif "ServiceUnavailable" in type(e).__name__ or "refused" in err.lower():
                logger.error(
                    f"Neo4j not reachable at {NEO4J['uri']}.\n"
                    "Options:\n"
                    "  1. Free cloud:  https://neo4j.com/cloud/aura-free/\n"
                    "  2. Local Docker: docker run -p 7474:7474 -p 7687:7687 "
                    "-e NEO4J_AUTH=neo4j/password neo4j:latest"
                )
            else:
                logger.error(f"Neo4j connection failed: {e}")
            self._driver = None
            self._connected = False
            return False

    def initialize_schema(self) -> None:
        """Create all constraints and indexes.

        Safe to run multiple times — every statement uses ``IF NOT EXISTS``.
        Call once at application startup after a successful ``connect()``.
        """
        if not self._connected:
            logger.warning("Cannot initialize schema — not connected to Neo4j")
            return
        for query in SCHEMA_QUERIES:
            try:
                self.query(query, {})
            except Exception as e:
                # Schema elements may already exist from a previous run — that's fine.
                logger.debug(f"Schema query note (usually harmless): {e}")
        logger.success("Neo4j schema initialized")

    def query(self, cypher: str, params: dict) -> list[dict]:
        """Execute a Cypher query and return results as a list of dicts.

        Args:
            cypher: Cypher query string.  Use ``$param_name`` placeholders.
            params: Parameter dict — values are safely bound, never interpolated.

        Returns:
            List of result records, each as a plain dict.

        Raises:
            RuntimeError: If called before a successful ``connect()``.
        """
        if not self._connected or self._driver is None:
            raise RuntimeError(
                "Neo4j not connected. Call connect() first or check your .env settings."
            )
        database = NEO4J.get("database") or None  # None → driver default
        with self._driver.session(database=database) as session:
            result = session.run(cypher, params)
            return [dict(record) for record in result]

    def query_single(self, cypher: str, params: dict) -> Optional[dict]:
        """Execute a query and return the first result record, or None.

        Args:
            cypher: Cypher query string.
            params: Parameter dict.

        Returns:
            First result as a dict, or None if the query returned no rows.
        """
        results = self.query(cypher, params)
        return results[0] if results else None

    def close(self) -> None:
        """Close the driver connection pool and reset internal state."""
        if self._driver:
            self._driver.close()
            self._driver = None
        self._connected = False
        logger.info("Neo4j connection closed")

    @property
    def is_connected(self) -> bool:
        """Return True if the client has an active Neo4j connection."""
        return self._connected