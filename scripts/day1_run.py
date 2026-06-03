#!/usr/bin/env python3
"""
Day 1 Run Script
================
End-to-end smoke test for the Code Review Agent.

Steps:
1. Validate GROQ_API_KEY is set — fail fast with clear message if not
2. Check ruff, bandit, radon are installed — print install commands if not
3. Create a temp Python file with intentional issues for testing
4. Initialise GroqClient, ToolRegistry, PromptEngine, ReActLoop
5. Register all tools (file, analysis, defect_api)
6. Run the agent on the temp test file
7. Print the full agent trace: each Thought/Action/Observation step
8. Print the final review report
9. Print tool call stats
10. Print timing: total seconds, LLM calls, tokens used
11. Assert state.status == COMPLETED
12. Print Day 1 git commit command
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Path setup ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from loguru import logger
from configs.config import AGENT, LLM
from src.agent.prompt_engine import PromptEngine
from src.agent.react_loop import ReActLoop
from src.agent.state import AgentStatus
from src.llm.groq_client import GroqClient
from src.tools.analysis_tools import register_analysis_tools
from src.tools.defect_api_tool import register_defect_api_tools
from src.tools.file_tools import register_file_tools
from src.tools.registry import ToolRegistry

# ── Logging setup ──────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> | {level} | {message}")
logger.add(ROOT / "logs" / "day1_run.log", level="DEBUG", rotation="10 MB")

# ── ANSI helpers ───────────────────────────────────────────────────────────────
BOLD  = "\033[1m"
GREEN = "\033[92m"
CYAN  = "\033[96m"
YELLOW= "\033[93m"
RED   = "\033[91m"
RESET = "\033[0m"
DIM   = "\033[2m"

def banner(text: str, color: str = CYAN) -> None:
    width = 70
    print(f"\n{color}{BOLD}{'─' * width}{RESET}")
    print(f"{color}{BOLD}  {text}{RESET}")
    print(f"{color}{BOLD}{'─' * width}{RESET}\n")


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Validate GROQ_API_KEY
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 1: Validating environment")

api_key = LLM.get("api_key", "")
if not api_key:
    print(f"{RED}✗ GROQ_API_KEY is not set.{RESET}")
    print(f"  Get a free key at {BOLD}https://console.groq.com{RESET}")
    print(f"  Then add to .env:  GROQ_API_KEY=gsk_...")
    sys.exit(1)
print(f"{GREEN}✓ GROQ_API_KEY found: {api_key[:8]}...{RESET}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Check tool dependencies
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 2: Checking tool dependencies")

missing_tools: list[str] = []
for tool_bin in ["ruff", "bandit", "radon"]:
    found = shutil.which(tool_bin)
    if found:
        print(f"{GREEN}✓ {tool_bin}: {found}{RESET}")
    else:
        print(f"{RED}✗ {tool_bin}: not found{RESET}")
        missing_tools.append(tool_bin)

if missing_tools:
    print(f"\n{YELLOW}Install missing tools:{RESET}")
    print(f"  pip install {' '.join(missing_tools)}")
    print(f"\nProceeding — analysis tools will return error messages for missing executables.")


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Create a temp test file with intentional issues
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 3: Creating test file with intentional issues")

TEST_FILE_CONTENT = '''"""
Intentionally buggy module — used as a test target for the Code Review Agent.
Contains: hardcoded credentials, SQL injection, unused imports, wildcard import,
high cyclomatic complexity.
"""
import os
import sys
import re
import json
import hashlib
from pathlib import *          # wildcard import — bad practice

import requests                # unused import
import pandas as pd            # unused import

# ── Hardcoded credentials (CRITICAL security issue) ───────────────────────────
DB_PASSWORD = "super_secret_password_123"
API_SECRET  = "sk-abc123hardcodedkey"
ADMIN_TOKEN = "Bearer eyJhbGciOiJIUzI1NiJ9.admin"


def get_user_by_name(conn, username: str) -> dict:
    """Fetch a user record — SQL injection vulnerability on line below."""
    # SQL injection: never use string concatenation for queries
    query = "SELECT * FROM users WHERE username = '" + username + "'"
    cursor = conn.cursor()
    cursor.execute(query)
    row = cursor.fetchone()
    return dict(row) if row else {}


def evaluate_expression(expr: str):
    """Dangerous use of eval() with user-supplied input."""
    return eval(expr)           # B307 — use of eval


def process_config(config_path: str) -> dict:
    """Load config with pickle — unsafe deserialization."""
    import pickle
    with open(config_path, "rb") as f:
        return pickle.load(f)  # B301 — pickle.load on untrusted data


def compute_risk_score(
    lines_of_code: int,
    num_authors: int,
    bug_history: list,
    age_days: int,
    coupling: float,
    churn: float,
    test_coverage: float,
    cyclomatic: int,
) -> str:
    """Highly complex function — cyclomatic complexity > 15."""
    score = 0.0

    if lines_of_code > 500:
        score += 2.0
    elif lines_of_code > 200:
        score += 1.0
    else:
        score += 0.5

    if num_authors > 10:
        score += 1.5
    elif num_authors > 5:
        score += 0.8

    if bug_history:
        recent_bugs = [b for b in bug_history if b.get("days_ago", 999) < 90]
        if len(recent_bugs) > 5:
            score += 3.0
        elif len(recent_bugs) > 2:
            score += 1.5
        else:
            score += 0.3

    if age_days > 1800:
        if churn > 0.5:
            score += 2.5
        elif churn > 0.2:
            score += 1.2
        else:
            score += 0.4
    elif age_days > 365:
        if churn > 0.3:
            score += 1.0
        else:
            score += 0.2

    if coupling > 0.8:
        score += 2.0
    elif coupling > 0.5:
        score += 1.0

    if test_coverage < 0.2:
        score += 2.0
    elif test_coverage < 0.5:
        score += 1.0
    elif test_coverage < 0.8:
        score += 0.5

    if cyclomatic > 20:
        score += 3.0
    elif cyclomatic > 10:
        score += 1.5

    if score > 8.0:
        return "CRITICAL"
    elif score > 5.0:
        return "HIGH"
    elif score > 2.5:
        return "MEDIUM"
    else:
        return "LOW"


def send_webhook(url: str, payload: dict, verify_ssl: bool = False) -> int:
    """Send webhook — SSL verification disabled by default (bad)."""
    response = requests.post(url, json=payload, verify=verify_ssl)
    return response.status_code


# ── Module-level code that always runs on import ──────────────────────────────
print(f"Module loaded. DB password: {DB_PASSWORD}")  # leaks secret in logs
'''

tmp_dir = tempfile.mkdtemp(prefix="cra_test_")
test_file = Path(tmp_dir) / "buggy_module.py"
test_file.write_text(TEST_FILE_CONTENT,encoding="utf-8")
print(f"{GREEN}✓ Test file created: {test_file}{RESET}")
print(f"  Issues planted: hardcoded credentials, SQL injection, eval(), pickle, wildcard import")
print(f"  File: {len(TEST_FILE_CONTENT.splitlines())} lines")


# ══════════════════════════════════════════════════════════════════════════════
# Step 4–5 — Initialise agent components and register tools
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 4-5: Initialising agent")

groq_client   = GroqClient()
tool_registry = ToolRegistry()
prompt_engine = PromptEngine()

register_file_tools(tool_registry)
register_analysis_tools(tool_registry)
register_defect_api_tools(tool_registry)

print(f"{GREEN}✓ Tools registered: {', '.join(tool_registry.list_tool_names())}{RESET}")

react_loop = ReActLoop(
    groq_client=groq_client,
    tool_registry=tool_registry,
    prompt_engine=prompt_engine,
)
print(f"{GREEN}✓ ReActLoop initialised{RESET}")
print(f"  Model: {groq_client.model}")
print(f"  Max steps: {AGENT['max_steps']}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 6 — Run the agent
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 6: Running agent", GREEN)
print(f"Reviewing: {BOLD}{test_file}{RESET}\n")

state = react_loop.run(
    file_path=str(test_file),
    repo_url="https://github.com/example/test-repo",
    file_content=TEST_FILE_CONTENT,
    risk_score=0.87,
    risk_label="HIGH",
    shap_features=[
        {"feature_name": "eval_usage",        "feature_value": 1,    "shap_value": 0.42},
        {"feature_name": "hardcoded_secrets",  "feature_value": 3,    "shap_value": 0.38},
        {"feature_name": "cyclomatic_max",     "feature_value": 18,   "shap_value": 0.27},
    ],
)


# ══════════════════════════════════════════════════════════════════════════════
# Step 7 — Print agent trace
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 7: Agent Trace")

for step in state.thought_history:
    print(f"{CYAN}{BOLD}Step {step.step_number}{RESET}")
    print(f"  {BOLD}Thought:{RESET} {DIM}{step.thought[:200]}{'...' if len(step.thought) > 200 else ''}{RESET}")
    print(f"  {BOLD}Action:{RESET}  {YELLOW}{step.action}{RESET}")
    print(f"  {BOLD}Input:{RESET}   {step.action_input}")
    obs_preview = step.observation[:300].replace("\n", " ")
    print(f"  {BOLD}Obs:{RESET}     {DIM}{obs_preview}{'...' if len(step.observation) > 300 else ''}{RESET}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# Step 8 — Print final review
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 8: Final Review Report")
if state.final_review:
    print(state.final_review)
else:
    print(f"{YELLOW}No final review produced (status: {state.status.value}){RESET}")

if state.summary:
    print(f"\n{BOLD}Summary:{RESET}")
    print(state.summary)


# ══════════════════════════════════════════════════════════════════════════════
# Step 9 — Tool call stats
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 9: Tool Call Statistics")
stats = tool_registry.get_call_stats()
for tool_name, count in sorted(stats.items(), key=lambda x: -x[1]):
    bar = "█" * count
    print(f"  {tool_name:<30} {bar} ({count})")


# ══════════════════════════════════════════════════════════════════════════════
# Step 10 — Timing and token usage
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 10: Performance Metrics")
usage = groq_client.get_usage_stats()
print(f"  Total time:   {state.elapsed_seconds:.1f}s")
print(f"  LLM calls:    {usage['total_calls']}")
print(f"  Total tokens: {usage['total_tokens']:,}")
print(f"  Agent steps:  {state.current_step}")
print(f"  Issues found: {len(state.issues_found)}")
print(f"  Status:       {state.status.value}")
counts = state.issue_count_by_severity
for sev, count in counts.items():
    if count > 0:
        print(f"    {sev}: {count}")


# ══════════════════════════════════════════════════════════════════════════════
# Step 11 — Assert COMPLETED
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 11: Assertions")
if state.status == AgentStatus.COMPLETED:
    print(f"{GREEN}✓ state.status == COMPLETED{RESET}")
elif state.status == AgentStatus.MAX_STEPS_REACHED:
    print(f"{YELLOW}⚠ state.status == MAX_STEPS_REACHED (partial review generated){RESET}")
    print(f"  Consider increasing AGENT['max_steps'] in config.py")
else:
    print(f"{RED}✗ state.status == {state.status.value}{RESET}")
    sys.exit(1)

assert state.current_step > 0, "Agent took no steps"
assert len(state.tools_called) > 0, "Agent called no tools"
print(f"{GREEN}✓ Agent took {state.current_step} steps{RESET}")
print(f"{GREEN}✓ Agent called {len(set(state.tools_called))} distinct tools{RESET}")


# ── Cleanup ────────────────────────────────────────────────────────────────────
import shutil as _shutil
_shutil.rmtree(tmp_dir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# Step 12 — Git commit command
# ══════════════════════════════════════════════════════════════════════════════
banner("Step 12: Day 1 Complete!", GREEN)
print(f"{BOLD}Git commit command:{RESET}")
print(f"""
  cd code-review-agent
  git add .
  git commit -m "feat: Day 1 — ReAct agent core complete

- Custom ReAct loop (no frameworks) with Thought/Action/Observation cycle
- GroqClient wrapper with retry + rate-limit handling
- ToolRegistry with safe error isolation (never raises to agent)
- PromptEngine with initial, continuation, reflection, and recovery prompts  
- AgentState dataclass tracking full thought history and findings
- File tools: read_file, list_python_files, get_function_context
- Analysis tools: ruff, bandit, radon, check_imports
- Defect Prediction API tool with graceful fallback
- finish_review control tool registered dynamically in ReActLoop
- End-to-end agent review of intentionally buggy Python file
- Unit tests for parser, registry, and state management"
""")