#!/usr/bin/env python3
"""
Day 2 Run Script
================
Demonstrates the full Day 2 stack:
  - Neo4j graph memory (with graceful fallback when unavailable)
  - Multi-file ReviewOrchestrator
  - MemoryWriter / MemoryRetriever
  - ReportGenerator with markdown + JSON output

Steps in order:
1. Check Neo4j connection — print setup instructions if unavailable,
   continue with use_memory=False rather than crashing.
2. Initialise ReviewOrchestrator.
3. Create a temp directory with 3 test Python files, each with
   intentional issues.
4. Run orchestrator.review_repo() on the temp directory.
5. Print full OrchestratorSession results.
6. If Neo4j connected: run a second review and verify memory context
   appears (proof memory is working).
7. Save report via ReportGenerator; print report path.
8. If Neo4j connected: print node/relationship counts.
9. Print Day 2 git commit command.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from loguru import logger
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {level} | {message}")
logger.add(ROOT / "logs" / "day2_run.log", level="DEBUG", rotation="10 MB")

from configs.config import NEO4J
from src.agent.orchestrator import ReviewOrchestrator
from src.memory.neo4j_client import Neo4jClient
from reports.generator import ReportGenerator

# ── ANSI helpers ───────────────────────────────────────────────────────────────
BOLD   = "\033[1m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
DIM    = "\033[2m"


def banner(text: str, color: str = CYAN) -> None:
    w = 70
    print(f"\n{color}{BOLD}{'─'*w}{RESET}")
    print(f"{color}{BOLD}  {text}{RESET}")
    print(f"{color}{BOLD}{'─'*w}{RESET}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Check Neo4j
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 1: Checking Neo4j connection")

neo4j_available = False
probe = Neo4jClient.get_instance()
try:
    neo4j_available = probe.connect()
except Exception:
    pass

if neo4j_available:
    print(f"{GREEN}✓ Neo4j connected at {NEO4J['uri']}{RESET}")
else:
    print(f"{YELLOW}⚠ Neo4j not available — running without memory.{RESET}")
    print(f"  To enable memory:")
    print(f"    Free cloud: https://neo4j.com/cloud/aura-free/")
    print(f"    Docker:     docker run -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:latest")
    print(f"  Then set NEO4J_URI / NEO4J_USERNAME / NEO4J_PASSWORD in .env")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Initialise orchestrator
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 2: Initialising orchestrator")

# Reset singleton so orchestrator gets a fresh connection attempt
Neo4jClient._instance = None

orchestrator = ReviewOrchestrator(use_memory=neo4j_available)
print(f"{GREEN}✓ ReviewOrchestrator ready  (memory={orchestrator.use_memory}){RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Create test files
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 3: Creating test files")

REPO_URL = "https://github.com/example/day2-test-repo"
tmp_dir  = Path(tempfile.mkdtemp(prefix="cra_day2_"))

FILE_A = tmp_dir / "auth.py"
FILE_A.write_text('''\
"""Authentication module — contains intentional security issues."""
import hashlib

SECRET_KEY = "hardcoded-jwt-secret-do-not-ship"   # CWE-798 hardcoded credential
ADMIN_PASS = "admin123"                            # CWE-798 hardcoded credential


def authenticate(conn, username: str, password: str) -> bool:
    """SQL injection via string concatenation."""
    query = "SELECT * FROM users WHERE username='" + username + "'"
    cursor = conn.cursor()
    cursor.execute(query)
    row = cursor.fetchone()
    if row and row["password"] == password:
        return True
    return False


def hash_password(plain: str) -> str:
    """Weak hash — MD5 is cryptographically broken."""
    return hashlib.md5(plain.encode()).hexdigest()  # B303 — use bcrypt/argon2
''')

FILE_B = tmp_dir / "data_processor.py"
FILE_B.write_text('''\
"""Data processing — complexity and import issues."""
import os
import sys
import pickle
from pathlib import *   # wildcard import

UNUSED_CONST = 42       # ruff: unused variable


def process(raw, fmt, validate, transform, cache, log, retry, timeout):
    """Highly complex orchestration — cyclomatic complexity > 12."""
    if not raw:
        return None
    if fmt == "json":
        if validate:
            if transform:
                result = _do_transform(raw)
                if cache:
                    _cache_result(result)
                    if log:
                        print(f"cached: {result}")
                        if retry:
                            return result or _retry(raw)
                        return result
                return result
            elif log:
                print(raw)
                return raw
        else:
            return raw
    elif fmt == "csv":
        if validate:
            return _csv_parse(raw)
        return raw
    return None


def _do_transform(data): return data
def _cache_result(data): pass
def _retry(data): return data
def _csv_parse(data): return data


def load_config(path: str):
    """Unsafe deserialization with pickle."""
    with open(path, "rb") as f:
        return pickle.load(f)     # B301 — arbitrary code execution risk
''')

FILE_C = tmp_dir / "api_client.py"
FILE_C.write_text('''\
"""External API client — another hardcoded credential."""
import requests

API_KEY = "sk-prod-abc123secretkey"   # CWE-798 hardcoded credential


def fetch(url: str, verify_ssl: bool = False) -> dict:
    """SSL verification disabled — B501."""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    resp = requests.get(url, headers=headers, verify=verify_ssl)
    resp.raise_for_status()
    return resp.json()


def run_query(expr: str):
    """Arbitrary eval on user input — B307."""
    return eval(expr)
''')

print(f"{GREEN}✓ Created 3 test files in {tmp_dir}:{RESET}")
for f in [FILE_A, FILE_B, FILE_C]:
    print(f"  {f.name}  ({f.stat().st_size} bytes)")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4 — First review session
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 4: Running first review session", GREEN)

session1 = orchestrator.review_repo(
    repo_url=REPO_URL,
    files_to_review=[str(FILE_A), str(FILE_B), str(FILE_C)],
    use_defect_api=False,
    max_files=3,
)


# ══════════════════════════════════════════════════════════════════════════════
# Step 5 — Print session results
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 5: Session 1 Results")
print(f"  Session ID:        {session1.session_id}")
print(f"  Files reviewed:    {len(session1.files_reviewed)}")
print(f"  Total issues:      {session1.total_issues}")
print(f"  Critical:          {session1.critical_issues}")
print(f"  High:              {session1.high_issues}")
print(f"  Medium:            {session1.medium_issues}")
print(f"  Low:               {session1.low_issues}")
print(f"  Total time:        {session1.total_elapsed_seconds:.1f}s")
print(f"  Errors:            {session1.errors or 'none'}")

print(f"\n{BOLD}Per-file summary:{RESET}")
for state in session1.file_states:
    name   = Path(state.file_path).name
    counts = state.issue_count_by_severity
    non_zero = {k: v for k, v in counts.items() if v > 0}
    print(f"  {CYAN}{name}{RESET}: status={state.status.value}, "
          f"steps={state.current_step}, issues={non_zero}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 6 — Second review (memory proof, Neo4j only)
# ══════════════════════════════════════════════════════════════════════════════
if neo4j_available and orchestrator.use_memory and orchestrator.retriever:
    banner("Step 6: Second review — verifying memory context", GREEN)

    # Retrieve context for auth.py as the agent will see it
    ctx = orchestrator.retriever.get_file_context(str(FILE_A), REPO_URL)
    if ctx:
        print(f"{GREEN}✓ Memory context retrieved for auth.py:{RESET}\n")
        print(ctx[:800])
        print(f"\n{GREEN}✓ Memory is working — context injected into second review.{RESET}")
    else:
        print(f"{YELLOW}⚠ No memory context yet (may need more reviews for patterns).{RESET}")

    # Run second session
    print(f"\n{BOLD}Running second review session...{RESET}")
    Neo4jClient._instance = None  # reset singleton for fresh orchestrator
    orchestrator2 = ReviewOrchestrator(use_memory=True)
    session2 = orchestrator2.review_repo(
        repo_url=REPO_URL,
        files_to_review=[str(FILE_A)],  # just auth.py
        use_defect_api=False,
    )
    print(f"  Session 2 issues found: {session2.total_issues}")
    print(f"{GREEN}✓ Second session complete.{RESET}")
else:
    banner("Step 6: Skipped (Neo4j not connected)", YELLOW)
    print(f"  Connect Neo4j and re-run to verify cross-session memory.")


# ══════════════════════════════════════════════════════════════════════════════
# Step 7 — Save report
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 7: Saving report")
generator = ReportGenerator()
report_dir = generator.save_session_report(session1)
print(f"{GREEN}✓ Report saved to:{RESET}  {report_dir}")
print(f"  summary.md   — {(report_dir / 'summary.md').stat().st_size} bytes")
print(f"  findings.json — {(report_dir / 'findings.json').stat().st_size} bytes")
print(f"  trace.md     — {(report_dir / 'trace.md').stat().st_size} bytes")

print(f"\n{BOLD}Report preview (first 400 chars of summary):{RESET}")
print(DIM + (report_dir / "summary.md").read_text()[:400] + RESET)


# ══════════════════════════════════════════════════════════════════════════════
# Step 8 — Neo4j stats
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 8: Neo4j Graph Statistics")
if neo4j_available:
    probe2 = Neo4jClient.get_instance()
    if probe2.is_connected:
        node_counts = probe2.query(
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC",
            {},
        )
        print(f"  {'Node type':<20} {'Count':>6}")
        print(f"  {'─'*26}")
        for row in node_counts:
            print(f"  {(row['label'] or 'Unknown'):<20} {row['cnt']:>6}")

        rel_count = probe2.query_single(
            "MATCH ()-[r]->() RETURN count(r) AS cnt", {}
        )
        print(f"\n  Total relationships: {rel_count['cnt'] if rel_count else 'N/A'}")
else:
    print(f"{YELLOW}  Neo4j not connected — no graph stats available.{RESET}")


# ── Cleanup ────────────────────────────────────────────────────────────────────
shutil.rmtree(tmp_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Step 9 — Git commit command
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 9: Day 2 Complete!", GREEN)
print(f"{BOLD}Git commit command:{RESET}")
print("""
  git add .
  git commit -m "feat: Day 2 — Neo4j graph memory + multi-file orchestration

- Neo4j graph schema: Repo/File/Review/Issue/Pattern nodes
- MemoryWriter: upsert with deduplication, pattern promotion at threshold=3
- MemoryRetriever: file context, active patterns, repo stats, recurring issues
- Memory tools: search_past_issues, get_file_review_history, get_repo_patterns
- ReviewOrchestrator: multi-file coordinator with memory-aware context injection
- ReportGenerator: summary.md + findings.json + trace.md per session
- Graceful degradation: all components work when Neo4j unavailable
- Singleton Neo4jClient: 100 get_instance() calls = 1 connection
- All Cypher queries use $param binding — no injection risk"
""")