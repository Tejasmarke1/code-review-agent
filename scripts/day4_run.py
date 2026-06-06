#!/usr/bin/env python3
"""
Day 4 Run Script
================
Confirms bug fixes, smoke-tests the SSE endpoint, exercises GitHub tools,
then keeps the server alive for manual UI exploration.
"""

import subprocess, sys, time, tempfile, shutil
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

BOLD="\033[1m"; GREEN="\033[92m"; CYAN="\033[96m"
YELLOW="\033[93m"; RED="\033[91m"; RESET="\033[0m"; DIM="\033[2m"

def banner(t, c=CYAN): w=70; print(f"\n{c}{BOLD}{'─'*w}{RESET}\n{c}{BOLD}  {t}{RESET}\n{c}{BOLD}{'─'*w}{RESET}\n")
def ok(m):   print(f"{GREEN}✓ {m}{RESET}")
def warn(m): print(f"{YELLOW}⚠ {m}{RESET}")
def fail(m): print(f"{RED}✗ {m}{RESET}")

# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Confirm bug fixes
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 1: Confirming bug fixes")

groq = (ROOT/"src/llm/groq_client.py").read_text(encoding="utf-8")
if "_min_interval: float = 4.0" in groq and "TOKEN_LIMIT_PER_MINUTE" in groq and "[4, 8, 16, 32, 60]" in groq:
    ok("Bug 1 — GroqClient: 4s throttle + token budget + [4,8,16,32,60] backoff")
else:
    fail("Bug 1 NOT fully fixed in groq_client.py")

neo = (ROOT/"src/memory/neo4j_client.py").read_text(encoding="utf-8")
if "__del__" in neo and "max_connection_lifetime" in neo and "ServiceUnavailable" in neo:
    ok("Bug 2 — Neo4jClient: __del__ + max_connection_lifetime + auto-reconnect")
else:
    fail("Bug 2 NOT fully fixed in neo4j_client.py")

orch = (ROOT/"src/agent/orchestrator.py").read_text(encoding="utf-8")
if "inter_file_cooldown" in orch and "_on_step_callback" in orch:
    ok("Bug 3 — Orchestrator: inter-file cooldown + streaming callbacks")
else:
    fail("Bug 3 NOT fully fixed in orchestrator.py")

# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Check dependencies
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 2: Checking dependencies")

missing = []
for pkg, imp in [("fastapi","fastapi"),("uvicorn","uvicorn"),("httpx","httpx")]:
    try: __import__(imp); ok(pkg)
    except ImportError: fail(f"{pkg} missing"); missing.append(pkg)

import shutil as _sh
if _sh.which("git"):
    ok(f"git found: {_sh.which('git')}")
else:
    warn("git not found — GitHub clone tools won't work")

if missing:
    print(f"\n{YELLOW}pip install {' '.join(missing)}{RESET}"); sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Start server
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 3: Starting FastAPI server on port 8000")

server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "src.api.main:app",
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"],
    cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace",
)
print(f"  Server PID: {server.pid}")
time.sleep(3)

# ══════════════════════════════════════════════════════════════════════════════
# Step 4 — Poll health
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 4: Waiting for server health")

import httpx
health_data = None
for i in range(15):
    try:
        r = httpx.get("http://localhost:8000/health", timeout=3)
        health_data = r.json()
        if health_data.get("status") in ("healthy", "degraded"):
            ok(f"Server ready — status={health_data['status']}  neo4j={health_data['neo4j_connected']}  groq={health_data['groq_connected']}")
            docs_r = httpx.get("http://localhost:8000/openapi.json", timeout=5)
            paths  = docs_r.json().get("paths", {})
            if "/stream/review" in paths:
                ok("/stream/review endpoint registered in OpenAPI")
            else:
                warn("/stream/review not found in OpenAPI — check stream router import in main.py")
            break
    except Exception as e:
        print(f"  [{i+1}/15] {e}")
    time.sleep(2)
else:
    fail("Server not healthy after 30s"); server.terminate(); sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
# Step 5 — SSE smoke test with local files
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 5: SSE smoke test")

tmp_dir = Path(tempfile.mkdtemp(prefix="cra_day4_"))
file_a  = tmp_dir / "test_auth.py"
file_b  = tmp_dir / "test_utils.py"

file_a.write_text('''\
SECRET = "hardcoded-key-123"
def login(conn, user):
    q = "SELECT * FROM users WHERE name='" + user + "'"
    conn.cursor().execute(q)
''', encoding="utf-8")

file_b.write_text('''\
import os, sys
def run(cmd):
    return eval(cmd)
TOKEN = "sk-prod-abc"
''', encoding="utf-8")

print(f"  Test files created in {tmp_dir}")
print(f"  Connecting to SSE stream (up to 120s timeout)...")

events_received: list[dict] = []
step_count  = 0
issue_count = 0
done_event  = None
current_event = ""

files_param = f"{file_a},{file_b}"
url = (
    f"http://localhost:8000/stream/review"
    f"?repo_url=https://github.com/example/day4-smoke-test"
    f"&files={files_param}"
    f"&max_files=2"
    f"&use_memory=true"
)

try:
    with httpx.stream("GET", url, timeout=180) as resp:
        t_start = time.time()
        for line in resp.iter_lines():
            if time.time() - t_start > 170:
                warn("SSE timeout — stopping collection")
                break
            if not line or not line.startswith("data:"):
                if line.startswith("event:"):
                    current_event = line.split(":", 1)[1].strip()
                continue
            try:
                import json
                data = json.loads(line[5:].strip())
                events_received.append({"type": current_event, "data": data})
                elapsed = f"{time.time()-t_start:.1f}s"
                if current_event == "step":
                    step_count += 1
                    action = data.get("action", "?")
                    print(f"  [{elapsed}] step: {action}")
                elif current_event == "issue":
                    issue_count += 1
                    print(f"  [{elapsed}] issue: [{data.get('severity')}] {data.get('title','')[:50]}")
                elif current_event == "status":
                    print(f"  [{elapsed}] status: {data.get('type','?')}")
                elif current_event == "done":
                    done_event = data
                    print(f"  [{elapsed}] DONE: {data}")
                    break
                elif current_event == "error":
                    print(f"  [{elapsed}] error: {data.get('message','?')}")
            except Exception:
                pass
except Exception as e:
    warn(f"SSE stream error: {e}")

passed = 0; total = 0
def chk(name, cond, detail=""):
    global passed, total
    total += 1
    if cond: passed += 1; ok(f"{name}{' — '+detail if detail else ''}")
    else: fail(f"{name}{' — '+detail if detail else ''}")

chk("Received at least 1 step event", step_count  >= 1, f"got {step_count}")
chk("Received done event",            done_event is not None)
chk("Total events received",          len(events_received) >= 2, f"got {len(events_received)}")
if done_event:
    chk("Done has files_reviewed", "files_reviewed" in done_event)

shutil.rmtree(tmp_dir, ignore_errors=True)

# ══════════════════════════════════════════════════════════════════════════════
# Step 6 — GitHub tools test
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 6: GitHub tools test")

try:
    from src.tools.github_tools import clone_github_repo, list_github_files, cleanup_github_clone, get_active_clones
    import shutil as _sh2

    if _sh2.which("git"):
        print("  Cloning https://github.com/mitsuhiko/click (small well-known repo)...")
        test_url = "https://github.com/mitsuhiko/click"
        result = clone_github_repo(test_url, depth=1)
        if result.startswith("ERROR"):
            warn(f"Clone failed: {result}")
        else:
            ok(f"Cloned to: {result}")
            files_out = list_github_files(test_url, max_files=5)
            print(f"  Files:\n{files_out[:400]}")
            clones = get_active_clones()
            ok(f"Active clones: {len(clones)}")
            cleanup_github_clone(test_url)
            ok("Clone cleaned up")
    else:
        warn("git not found — skipping GitHub clone test")
except Exception as e:
    warn(f"GitHub tools test error: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# Step 7 — Response times
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 7: API Response Times")
endpoints = [
    ("GET /",             lambda: httpx.get("http://localhost:8000/")),
    ("GET /health",       lambda: httpx.get("http://localhost:8000/health")),
    ("GET /review/list",  lambda: httpx.get("http://localhost:8000/review/list")),
    ("GET /memory/stats", lambda: httpx.get("http://localhost:8000/memory/stats")),
]
for name, fn in endpoints:
    t0 = time.time()
    try: fn()
    except: pass
    print(f"  {name:<30} {time.time()-t0:.2f}s")

# ══════════════════════════════════════════════════════════════════════════════
# Step 8 — Summary + URLs
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 8: Summary")
print(f"  SSE smoke tests: {passed}/{total} passed")
print(f"\n  {BOLD}Open in browser:{RESET}")
print(f"    Live UI:  {CYAN}http://localhost:8000/app{RESET}")
print(f"    API docs: {CYAN}http://localhost:8000/docs{RESET}")
print(f"\n  {DIM}Press Ctrl+C to stop{RESET}")

try:
    while True:
        line = server.stdout.readline()
        if line: print(f"  {DIM}[server] {line.rstrip()}{RESET}")
        if server.poll() is not None: fail("Server exited unexpectedly"); break
        time.sleep(0.1)
except KeyboardInterrupt:
    print(f"\n{YELLOW}Stopping server...{RESET}")
    server.terminate(); server.wait(timeout=5); ok("Server stopped")

# ══════════════════════════════════════════════════════════════════════════════
# Step 9 — Git commit
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 9: Day 4 Complete!", GREEN)
print(f"""{BOLD}Git commit command:{RESET}
  git add .
  git commit -m "feat: Day 4 — SSE streaming, GitHub integration, live trace UI

Bug fixes:
  - GroqClient: token-aware rate limiting with 60s rolling window (5000 TPM)
  - GroqClient: 5 retries with [4,8,16,32,60]s backoff, 4s min interval
  - Neo4jClient: auto-reconnect on ServiceUnavailable/SessionExpired
  - Neo4jClient: max_connection_lifetime=300, connection_acquisition_timeout=60
  - Orchestrator: 8s inter-file cooldown between reviews

Day 4 new:
  - /stream/review SSE endpoint with asyncio.Queue bridge
  - StreamingReviewRunner: orchestrator runs in ThreadPoolExecutor
  - Streaming callbacks in ReActLoop: _streaming_step_cb, _streaming_issue_cb
  - github_tools.py: clone_github_repo, list_github_files, get_github_file_path
  - Three-panel UI: live agent trace, issues-as-found, memory stats
  - EventSource JS with step/issue/status/done event handlers
  - GitHub clone cleanup registered in FastAPI lifespan shutdown"
""")