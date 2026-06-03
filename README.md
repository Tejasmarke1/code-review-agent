# Autonomous Code Review Agent

A production-grade AI agent that autonomously reviews Python code using a custom ReAct loop, real static analysis tools, and a GNN+XGBoost Defect Prediction Engine.

**No LangChain. No LlamaIndex. No AutoGen. Pure Python.**

---

## Architecture

```
code-review-agent/
├── configs/config.py          # All settings — no magic numbers
├── src/
│   ├── agent/
│   │   ├── react_loop.py      # Core ReAct engine (Think→Act→Observe)
│   │   ├── prompt_engine.py   # All prompts as typed functions
│   │   └── state.py           # AgentState + ReviewIssue dataclasses
│   ├── tools/
│   │   ├── registry.py        # Tool routing with safe error isolation
│   │   ├── file_tools.py      # read_file, list_python_files, get_function_context
│   │   ├── analysis_tools.py  # ruff, bandit, radon, check_imports
│   │   └── defect_api_tool.py # Defect Prediction API integration
│   ├── llm/
│   │   └── groq_client.py     # Groq API wrapper with retry logic
│   └── memory/                # Day 3 — Neo4j graph memory
├── notebooks/
│   └── day1_agent_test.ipynb  # End-to-end demo notebook
├── scripts/
│   └── day1_run.py            # CLI smoke test
└── tests/
    └── test_react_loop.py     # Unit tests (no LLM calls needed)
```

---

## How the ReAct Loop Works

```
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

## Day 1 — What's Built

| Component | File | Status |
|---|---|---|
| ReAct loop engine | `src/agent/react_loop.py` | ✅ |
| Prompt construction | `src/agent/prompt_engine.py` | ✅ |
| Agent state | `src/agent/state.py` | ✅ |
| Tool registry | `src/tools/registry.py` | ✅ |
| File tools | `src/tools/file_tools.py` | ✅ |
| Analysis tools | `src/tools/analysis_tools.py` | ✅ |
| Defect API tool | `src/tools/defect_api_tool.py` | ✅ |
| Groq LLM client | `src/llm/groq_client.py` | ✅ |
| Unit tests | `tests/test_react_loop.py` | ✅ |

## Day 3 — Coming Next

- Neo4j graph memory (`src/memory/`)
- Cross-session pattern detection
- Recurring issue tracking

## Day 4 — Planned

- FastAPI review endpoint
- Streaming trace output

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Get a free Groq API key

Go to [https://console.groq.com](https://console.groq.com) — no credit card required.

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and add your GROQ_API_KEY
```

### 4. Run the Day 1 smoke test

```bash
python scripts/day1_run.py
```

### 5. Run unit tests

```bash
pytest tests/test_react_loop.py -v
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
| `finish_review` | control | Signal review complete |

---

## Design Decisions

**Why no frameworks?** LangChain/LlamaIndex add abstraction layers that hide what the agent is actually doing. Rolling your own ReAct loop takes ~300 lines but gives you complete control over prompt construction, stop conditions, error handling, and context management.

**Why Groq?** Fastest inference on the market, free tier, OpenAI-compatible API. `llama-3.3-70b-versatile` gives strong reasoning quality at near-zero cost.

**Why is `ToolRegistry.call()` guaranteed not to raise?** The agent loop should never crash because a tool had an unexpected error. Every exception becomes an observation string. The agent reads the error and decides what to do next — same as a human developer reading a stack trace.

**Why is `finish_review` registered in `react_loop.py`?** Control flow belongs to the loop engine, not the tool files. Separating concerns makes the tool files reusable in other contexts.

---

## Reference

> Yao, S. et al. (2022). ReAct: Synergizing Reasoning and Acting in Language Models. [arXiv:2210.03629](https://arxiv.org/abs/2210.03629)