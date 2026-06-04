"""
Neo4j Graph Schema Definitions
================================
All node labels, relationship types, property names, and
Cypher constraint/index creation in one place.

Never hardcode label names or property names anywhere else —
always import from here.
"""


# ── Node Labels ────────────────────────────────────────────────
class NodeLabel:
    REPO = "Repo"
    FILE = "File"
    REVIEW = "Review"
    ISSUE = "Issue"
    PATTERN = "Pattern"


# ── Relationship Types ─────────────────────────────────────────
class RelType:
    CONTAINS = "CONTAINS"
    HAS_REVIEW = "HAS_REVIEW"
    FOUND = "FOUND"
    HAS_ISSUE = "HAS_ISSUE"
    PART_OF_PATTERN = "PART_OF_PATTERN"
    RECURS_IN = "RECURS_IN"
    AFFECTS = "AFFECTS"


# ── Cypher queries to create constraints and indexes ───────────
# Run these once at startup via Neo4jClient.initialize_schema()
# IF NOT EXISTS makes all queries idempotent.
SCHEMA_QUERIES = [
    # Uniqueness constraints
    "CREATE CONSTRAINT repo_url IF NOT EXISTS FOR (r:Repo) REQUIRE r.url IS UNIQUE",
    "CREATE CONSTRAINT review_session IF NOT EXISTS FOR (rv:Review) REQUIRE rv.session_id IS UNIQUE",
    "CREATE CONSTRAINT issue_id IF NOT EXISTS FOR (i:Issue) REQUIRE i.issue_id IS UNIQUE",
    "CREATE CONSTRAINT pattern_id IF NOT EXISTS FOR (p:Pattern) REQUIRE p.pattern_id IS UNIQUE",

    # Indexes for fast lookup
    "CREATE INDEX file_path IF NOT EXISTS FOR (f:File) ON (f.path, f.repo_url)",
    "CREATE INDEX issue_category IF NOT EXISTS FOR (i:Issue) ON (i.category, i.severity)",
    "CREATE INDEX issue_file IF NOT EXISTS FOR (i:Issue) ON (i.file_path, i.repo_url)",
]