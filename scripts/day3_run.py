#!/usr/bin/env python3
"""
Day 3 Run Script
================
Validates the 3 bug fixes, smoke-tests the FastAPI server, and keeps
the server alive for manual UI exploration.

Steps:
1. Confirm bug fixes are present in source files
2. Validate API dependencies installed (fastapi, uvicorn, httpx)
3. Start FastAPI server in a subprocess on port 8000
4. Poll GET /health until healthy or 30s timeout
5. Run API smoke tests via httpx
6. Print all response times
7. Print UI and docs URLs
8. Keep server running until Ctrl+C
9. Print Day 3 git commit command
"""

import subprocess
import sys
import time
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# ── ANSI ───────────────────────────────────────────────────────────────────────
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


def ok(msg: str)   -> None: print(f"{GREEN}✓ {msg}{RESET}")
def warn(msg: str) -> None: print(f"{YELLOW}⚠ {msg}{RESET}")
def fail(msg: str) -> None: print(f"{RED}✗ {msg}{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Confirm bug fixes
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 1: Confirming bug fixes")

groq_src = (ROOT / "src" / "llm" / "groq_client.py").read_text(encoding="utf-8")
if "_min_interval" in groq_src and "4 * (2 ** attempt)" in groq_src:
    ok("Bug 1 fixed — GroqClient throttle + longer backoff present")
else:
    fail("Bug 1 NOT fixed — missing _min_interval or updated backoff in groq_client.py")

neo4j_src = (ROOT / "src" / "memory" / "neo4j_client.py").read_text(encoding="utf-8")
if "__del__" in neo4j_src and "session.close()" in neo4j_src:
    ok("Bug 2 fixed — Neo4jClient __del__ and session.close() present")
else:
    fail("Bug 2 NOT fixed — missing __del__ or session.close() in neo4j_client.py")

gen_src = (ROOT / "reports" / "generator.py").read_text(encoding="utf-8")
utf8_count = gen_src.count('encoding=_ENC') + gen_src.count('encoding="utf-8"')
if utf8_count >= 4:
    ok(f"Bug 3 fixed — {utf8_count} explicit UTF-8 write calls in generator.py")
else:
    warn(f"Bug 3 partial — only {utf8_count} UTF-8 write calls found (expected 4+)")


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Check API dependencies
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 2: Checking API dependencies")

missing = []
for pkg, import_name in [
    ("fastapi",          "fastapi"),
    ("uvicorn",          "uvicorn"),
    ("httpx",            "httpx"),
    ("python-multipart", "multipart"),
]:
    try:
        __import__(import_name)
        ok(f"{pkg}")
    except ImportError:
        fail(f"{pkg} not installed")
        missing.append(pkg)

if missing:
    print(f"\n{YELLOW}Install missing packages:{RESET}")
    print(f"  pip install {' '.join(missing)}")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Start FastAPI server
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 3: Starting FastAPI server on port 8000")

server_proc = subprocess.Popen(
    [
        sys.executable, "-m", "uvicorn",
        "src.api.main:app",
        "--host", "0.0.0.0",
        "--port", "8000",
        "--log-level", "warning",
    ],
    cwd=str(ROOT),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
)
print(f"  Server PID: {server_proc.pid}")
time.sleep(2)


# ══════════════════════════════════════════════════════════════════════════════
# Step 4 — Poll /health until ready
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 4: Waiting for server to be ready")

import httpx  # imported after install check

health_data = None
for attempt in range(15):
    try:
        r = httpx.get("http://localhost:8000/health", timeout=3)
        health_data = r.json()
        status = health_data.get("status", "unknown")
        if status in ("healthy", "degraded"):
            ok(f"Server ready — status={status}  neo4j={health_data['neo4j_connected']}  groq={health_data['groq_connected']}")
            break
        warn(f"Status={status}, retrying...")
    except Exception as e:
        print(f"  [{attempt+1}/15] waiting... ({e})")
    time.sleep(2)
else:
    fail("Server did not become healthy within 30s")
    server_proc.terminate()
    sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# Step 5 — API smoke tests
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 5: API smoke tests")

# Create two temp test files
tmp_dir = Path(tempfile.mkdtemp(prefix="cra_day3_"))
file_a = tmp_dir / "test_auth.py"
file_b = tmp_dir / "test_utils.py"

file_a.write_text('''\
SECRET = "hardcoded-key-123"   # CWE-798
def login(conn, user):
    q = "SELECT * FROM users WHERE name='" + user + "'"
    conn.cursor().execute(q)
''', encoding="utf-8")

file_b.write_text('''\
import os, sys, re  # unused
def run(cmd):
    return eval(cmd)   # B307
''', encoding="utf-8")

timings: dict[str, float] = {}
passed = 0
total  = 0

def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    if condition:
        passed += 1
        ok(f"{name}{' — ' + detail if detail else ''}")
    else:
        fail(f"{name}{' — ' + detail if detail else ''}")

# ── GET / ─────────────────────────────────────────────────────
t0 = time.time()
r = httpx.get("http://localhost:8000/")
timings["GET /"] = time.time() - t0
check("GET /", r.status_code == 200, f"status={r.status_code}")

# ── GET /health ───────────────────────────────────────────────
t0 = time.time()
r = httpx.get("http://localhost:8000/health")
timings["GET /health"] = time.time() - t0
data = r.json()
check("GET /health", r.status_code == 200 and data["status"] in ("healthy", "degraded"),
      f"status={data.get('status')}")

# ── POST /review/ ─────────────────────────────────────────────
print(f"\n  {DIM}Running agent review on 2 test files (may take 30-120s)...{RESET}")
t0 = time.time()
try:
    r = httpx.post(
        "http://localhost:8000/review/",
        json={
            "repo_url": "https://github.com/example/day3-smoke-test",
            "files": [str(file_a), str(file_b)],
            "max_files": 2,
            "use_memory": True,
            "use_defect_api": False,
        },
        timeout=300,
    )
    timings["POST /review/"] = time.time() - t0
    review_data = r.json() if r.status_code == 200 else {}
    session_id  = review_data.get("session_id", "")
    check("POST /review/", r.status_code == 200, f"session_id={session_id}")
    check("Review has session_id", bool(session_id))
    check("Review has file_results", len(review_data.get("file_results", [])) == 2,
          f"got {len(review_data.get('file_results', []))}")
except Exception as e:
    timings["POST /review/"] = time.time() - t0
    fail(f"POST /review/ — {e}")
    session_id = ""

# ── GET /review/{id} ──────────────────────────────────────────
if session_id:
    t0 = time.time()
    r = httpx.get(f"http://localhost:8000/review/{session_id}")
    timings[f"GET /review/{{id}}"] = time.time() - t0
    check("GET /review/{id}", r.status_code == 200, f"status={r.status_code}")

# ── GET /review/bad-id ────────────────────────────────────────
t0 = time.time()
r = httpx.get("http://localhost:8000/review/nonexistent-abc123")
timings["GET /review/bad-id"] = time.time() - t0
check("GET /review/bad-id → 404", r.status_code == 404)

# ── GET /review/list ──────────────────────────────────────────
t0 = time.time()
r = httpx.get("http://localhost:8000/review/list")
timings["GET /review/list"] = time.time() - t0
check("GET /review/list", r.status_code == 200, f"count={len(r.json())}")

# ── GET /memory/stats ─────────────────────────────────────────
t0 = time.time()
r = httpx.get("http://localhost:8000/memory/stats")
timings["GET /memory/stats"] = time.time() - t0
check("GET /memory/stats", r.status_code == 200,
      f"connected={r.json().get('connected')}")

# ── GET /memory/patterns ──────────────────────────────────────
t0 = time.time()
r = httpx.get(
    "http://localhost:8000/memory/patterns",
    params={"repo_url": "https://github.com/example/day3-smoke-test"},
)
timings["GET /memory/patterns"] = time.time() - t0
check("GET /memory/patterns", r.status_code == 200,
      f"patterns={r.json().get('total', 0)}")

# Cleanup temp files
shutil.rmtree(tmp_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Step 6 — Response times
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 6: Response Times")
for endpoint, elapsed in timings.items():
    bar = "█" * max(1, int(elapsed * 2))
    print(f"  {endpoint:<30} {bar} {elapsed:.2f}s")


# ══════════════════════════════════════════════════════════════════════════════
# Step 7 — Print test summary + URLs
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 7: Summary")
print(f"  Tests passed: {passed}/{total}")
print(f"\n  {BOLD}Open in browser:{RESET}")
print(f"    UI:   {CYAN}http://localhost:8000/app{RESET}")
print(f"    Docs: {CYAN}http://localhost:8000/docs{RESET}")
print(f"    API:  {CYAN}http://localhost:8000/{RESET}")
print()
print(f"  {DIM}Press Ctrl+C to stop the server{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 8 — Keep server alive
# ══════════════════════════════════════════════════════════════════════════════
try:
    while True:
        # Stream server output
        line = server_proc.stdout.readline()
        if line:
            print(f"  {DIM}[server] {line.rstrip()}{RESET}")
        if server_proc.poll() is not None:
            fail("Server process died unexpectedly")
            break
        time.sleep(0.1)
except KeyboardInterrupt:
    print(f"\n\n{YELLOW}Stopping server...{RESET}")
    server_proc.terminate()
    server_proc.wait(timeout=5)
    ok("Server stopped")


# ══════════════════════════════════════════════════════════════════════════════
# Step 9 — Git commit
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 9: Day 3 Complete!", GREEN)
print(f"{BOLD}Git commit command:{RESET}")
print("""
  git add .
  git commit -m "feat: Day 3 — FastAPI + UI + bug fixes

Bug fixes:
  - GroqClient: 2s inter-call throttle + [4,8,16]s retry backoff
  - Neo4jClient: __del__ clean shutdown + session.close() in finally
  - ReportGenerator: explicit encoding=utf-8 on all write_text() calls

Day 3 new:
  - FastAPI app with /review, /memory, /health routers
  - AppState singleton loaded once at startup via lifespan
  - POST /review/ runs orchestrator in ThreadPoolExecutor (non-blocking)
  - GET /memory/stats|file|patterns|DELETE /memory/repo
  - Vanilla JS UI served at /app — repo URL input, issues display, memory panel
  - Pydantic v2 request/response models with validators
  - TestClient-based API tests with full mocking (no real LLM calls)"
""")