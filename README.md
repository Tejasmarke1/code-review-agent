# Autonomous Code Review Agent

A production-grade AI agent that autonomously reviews Python code using a custom ReAct loop, real static analysis tools, and Neo4j Graph Memory for cross-session intelligence and pattern detection.

**No LangChain. No LlamaIndex. No AutoGen. Pure Python.**

---

## Architecture

```text
code-review-agent/
├── configs/config.py          # All settings — no magic numbers
├── src/
│   ├── agent/
│   │   ├── orchestrator.py    # Multi-file Review Orchestrator
│   │   ├── react_loop.py      # Core ReAct engine (Think→Act→Observe)
│   │   ├── prompt_engine.py   # All prompts as typed functions
│   │   └── state.py           # AgentState + ReviewIssue dataclasses
│   ├── api/                   # FastAPI Server
│   │   ├── main.py
│   │   ├── models.py
│   │   └── routers/
│   ├── llm/
│   │   └── groq_client.py     # Groq API wrapper with retry logic
│   ├── memory/                # Neo4j graph memory
│   │   ├── neo4j_client.py
│   │   ├── memory_writer.py
│   │   ├── memory_retriever.py
│   │   └── graph_schema.py
│   └── tools/
│       ├── registry.py        # Tool routing with safe error isolation
│       ├── file_tools.py      # read_file, list_python_files, get_function_context
│       ├── analysis_tools.py  # ruff, bandit, radon, check_imports
│       ├── memory_tools.py    # search_past_issues, get_repo_patterns
│       └── defect_api_tool.py # Defect Prediction API integration
├── ui/                        # Web Interface
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── notebooks/
│   ├── day1_agent_test.ipynb  # End-to-end demo notebook
│   └── day2_memory_test.ipynb # Memory integration notebook
├── reports/
│   └── generator.py           # Markdown & JSON report generation
├── scripts/
│   ├── day1_run.py            # CLI smoke test
│   ├── day2_run.py            # Full stack multi-file review & Memory demo
│   └── day3_run.py            # API Server & UI demo
└── tests/
    ├── test_memory.py         # Memory integration tests
    └── test_react_loop.py     # Unit tests (no LLM calls needed)
```

---

## How the ReAct Loop Works

```text
┌─────────────────────────────────────────────────────────────┐
│                        ReAct Loop                           │
│                                                             │
│   ┌──────────┐     ┌──────────┐     ┌────────────────┐    │
│   │ THOUGHT  │────▶│  ACTION  │────▶│  OBSERVATION   │    │
│   │          │     │          │     │                │    │
│   │ "I should│     │ run_ruff │     │ "Found 5 style │    │
│   │  check   │     │ (/tmp/   │     │  violations at │    │
│   │  linting"│     │  x.py)   │     │  lines 12,19.."│    │
│   └──────────┘     └──────────┘     └───────┬────────┘    │
│        ▲                                     │             │
│        └─────────────────────────────────────┘             │
│                                                             │
│   Stop conditions:                                          │
│     • Agent calls finish_review                            │
│     • max_steps reached                                    │
│     • 3 consecutive parse failures                         │
└─────────────────────────────────────────────────────────────┘
```

The loop is implemented from scratch in `src/agent/react_loop.py`. No frameworks.

---

## What's Built (Days 1 - 3)

| Component | File | Status |
|---|---|---|
| ReAct loop engine | `src/agent/react_loop.py` | ✅ |
| Prompt construction | `src/agent/prompt_engine.py` | ✅ |
| Agent state & Memory | `src/agent/state.py` | ✅ |
| Tool registry | `src/tools/registry.py` | ✅ |
| File tools | `src/tools/file_tools.py` | ✅ |
| Analysis tools | `src/tools/analysis_tools.py` | ✅ |
| Defect API tool | `src/tools/defect_api_tool.py` | ✅ |
| Groq LLM client | `src/llm/groq_client.py` | ✅ |
| Multi-file Orchestrator | `src/agent/orchestrator.py` | ✅ |
| Neo4j graph memory | `src/memory/` | ✅ |
| Memory Query Tools | `src/tools/memory_tools.py` | ✅ |
| Markdown & JSON Reports | `reports/generator.py` | ✅ |
| FastAPI Server | `src/api/` | ✅ |
| Web Interface (UI) | `ui/` | ✅ |
| Automated Tests | `tests/` | ✅ |

The system now operates across multiple files within repositories, automatically remembering past issues, tracking reviews across time, detecting systemic patterns via Neo4j Graph Memory, and serving everything via a modern FastAPI layer + Web Interface.

## Day 4 — Planned

- Streaming trace output
- Defect API live integrations
- Pipeline/CI integrations

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get a free Groq API key

Go to [https://console.groq.com](https://console.groq.com) — no credit card required.

### 3. Setup Neo4j Database

You can run it locally with Docker:
```bash
docker run -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/password neo4j:latest
```
Or create a free cloud instance on [Neo4j Aura](https://neo4j.com/cloud/aura-free/).

### 4. Configure environment

```bash
cp .env.example .env
# Edit .env and add your GROQ_API_KEY, NEO4J_URI, NEO4J_USERNAME, and NEO4J_PASSWORD
```

### 5. Run the Smoke Tests & Server

Day 1 (Agent Loop Test):
```bash
python scripts/day1_run.py
```

Day 2 (Multi-file Orchestrator & Memory Test):
```bash
python scripts/day2_run.py
```

Day 3 (FastAPI Server & Web UI Test):
```bash
python scripts/day3_run.py
```

*When Day 3 script is running, the server is available at `http://localhost:8000` and the Web UI at `http://localhost:8000/ui/index.html`.*

### 6. Run unit tests

```bash
pytest tests/ -v
```

---

## Tools Available to the Agent

| Tool | Category | Purpose |
|---|---|---|
| `read_file` | file | Read Python source with line numbers |
| `list_python_files` | file | Explore repo structure |
| `get_function_context` | file | Extract specific function source |
| `run_ruff` | analysis | Style + linting violations |
| `run_bandit` | analysis | Security vulnerability scanning |
| `run_radon` | analysis | Cyclomatic complexity |
| `check_imports` | analysis | Dependency and coupling analysis |
| `get_repo_risk_scores` | defect_api | ML risk scores for all files |
| `get_file_explanation` | defect_api | SHAP explanation for one file |
| `search_past_issues` | memory | Search Neo4j memory for past issues in repo |
| `get_file_review_history` | memory | Get historical review trends for a file |
| `get_repo_patterns` | memory | Detect systemic cross-file patterns |
| `finish_review` | control | Signal review complete |

---

## Design Decisions

**Why no frameworks?** LangChain/LlamaIndex add abstraction layers that hide what the agent is actually doing. Rolling your own ReAct loop takes ~300 lines but gives you complete control over prompt construction, stop conditions, error handling, and context management.

**Why Groq?** Fastest inference on the market, free tier, OpenAI-compatible API. `llama-3.3-70b-versatile` gives strong reasoning quality at near-zero cost.

**Why Neo4j?** Code structure is fundamentally a graph. Files, classes, methods, imports, issues, and patterns all form a highly-connected network perfectly suited for graph databases rather than relational or purely vector databases. 

**Why is `ToolRegistry.call()` guaranteed not to raise?** The agent loop should never crash because a tool had an unexpected error. Every exception becomes an observation string. The agent reads the error and decides what to do next — same as a human developer reading a stack trace.

---

## Reference

> Yao, S. et al. (2022). ReAct: Synergizing Reasoning and Acting in Language Models. [arXiv:2210.03629](https://arxiv.org/abs/2210.03629)