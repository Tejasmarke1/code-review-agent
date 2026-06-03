"""Central config — all settings live here, no magic numbers anywhere."""

from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

ROOT_DIR = Path(__file__).parent.parent

# ── LLM ───────────────────────────────────────────────────────
LLM = {
    "provider": "groq",
    "model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
    "api_key": os.getenv("GROQ_API_KEY", ""),
    "api_base": "https://api.groq.com/openai/v1",
    "max_tokens": int(os.getenv("GROQ_MAX_TOKENS", 4096)),
    "temperature": float(os.getenv("GROQ_TEMPERATURE", 0.1)),
    "timeout": int(os.getenv("GROQ_TIMEOUT_SECONDS", 30)),
}

# ── Agent ──────────────────────────────────────────────────────
AGENT = {
    "max_steps": 15,            # max ReAct iterations before forced stop
    "max_retries": 3,           # retries on LLM parse failure
    "reflection_interval": 3,   # reflect every N tool calls
    "min_severity_to_report": "LOW",   # LOW / MEDIUM / HIGH / CRITICAL
    "tool_timeout_seconds": 30,
}

# ── Tools ──────────────────────────────────────────────────────
TOOLS = {
    "ruff_executable": "ruff",
    "bandit_executable": "bandit",
    "max_file_size_bytes": 150_000,
    "max_files_per_review": 10,
    "supported_extensions": [".py"],
}

# ── Defect Prediction API ──────────────────────────────────────
DEFECT_API = {
    "base_url": os.getenv("DEFECT_API_URL", "http://localhost:8000"),
    "timeout_seconds": 60,
    "top_k": 20,
    "min_risk_score": 0.4,      # ignore files below this threshold
    "use_hybrid": True,
}

# ── Neo4j Memory (Day 3) ───────────────────────────────────────
NEO4J = {
    "uri": os.getenv("NEO4J_URI", "bolt://localhost:7687"),
    "username": os.getenv("NEO4J_USERNAME", "neo4j"),
    "password": os.getenv("NEO4J_PASSWORD", ""),
    "database": os.getenv("NEO4J_DATABASE", "neo4j"),
}

# ── Output ─────────────────────────────────────────────────────
OUTPUT = {
    "reports_dir": ROOT_DIR / "reports",
    "logs_dir": ROOT_DIR / "logs",
}

for d in [OUTPUT["reports_dir"], OUTPUT["logs_dir"]]:
    d.mkdir(parents=True, exist_ok=True)