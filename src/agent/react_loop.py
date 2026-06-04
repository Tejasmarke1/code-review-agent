"""
ReAct Loop — Core Agent Engine
================================
Implements the Reasoning + Acting loop from scratch.

The loop:
    1. Build prompt with current state
    2. Call LLM → get Thought + Action + Action Input
    3. Parse the response
    4. Call the tool via registry
    5. Get observation
    6. Update state
    7. Check stop conditions
    8. Repeat

Stop conditions:
    - Agent calls finish_review tool
    - Max steps reached
    - LLM parse fails 3 times in a row
    - Tool returns FATAL error

Reference: "ReAct: Synergizing Reasoning and Acting in Language Models"
Yao et al., 2022 — https://arxiv.org/abs/2210.03629
"""

import json
import re
import uuid
from datetime import datetime
from typing import Optional
from loguru import logger
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import AGENT
from src.agent.state import AgentState, AgentStatus, ReviewIssue, Severity
from src.agent.prompt_engine import PromptEngine
from src.llm.groq_client import GroqClient
from src.tools.registry import Tool, ToolRegistry


class ReActLoop:
    """
    Core ReAct agent engine.

    One ReActLoop instance handles one file review session.
    Stateless between sessions — create a new instance per review.
    """

    FINISH_TOOL = "finish_review"

    def __init__(
        self,
        groq_client: GroqClient,
        tool_registry: ToolRegistry,
        prompt_engine: PromptEngine,
    ):
        """Initialise the ReAct loop with its three collaborators.

        Args:
            groq_client: Configured GroqClient for LLM calls.
            tool_registry: ToolRegistry containing all available tools.
            prompt_engine: PromptEngine for constructing all prompt variants.
        """
        self.llm = groq_client
        self.tools = tool_registry
        self.prompt = prompt_engine
        self._consecutive_parse_failures = 0

    def run(
        self,
        file_path: str,
        repo_url: str,
        file_content: str,
        risk_score: float = 0.0,
        risk_label: str = "UNKNOWN",
        shap_features: Optional[list[dict]] = None,
        past_issues_context: str = "",
    ) -> AgentState:
        """Run the full ReAct loop for one file review.

        Creates a fresh AgentState, then iterates Thought→Action→Observation
        cycles until the agent calls finish_review, max steps is reached, or
        too many consecutive parse failures occur.

        Args:
            file_path: Path to the file being reviewed.
            repo_url: Repository URL (used for context in prompts).
            file_content: The file's source code as a string.
            risk_score: ML risk score from the Defect Prediction API (0.0–1.0).
            risk_label: Human-readable risk label — HIGH / MEDIUM / LOW / UNKNOWN.
            shap_features: Top SHAP feature dicts from the ML model (optional).
            past_issues_context: Relevant past issues from Neo4j memory (optional).

        Returns:
            Completed AgentState with all findings, thought history, and final review.
        """
        state = AgentState(
            session_id=str(uuid.uuid4())[:8],
            repo_url=repo_url,
            file_path=file_path,
            file_content=file_content,
            risk_score=risk_score,
            risk_label=risk_label,
            shap_features=shap_features or [],
            past_issues_context=past_issues_context,
        )

        logger.info(
            f"[{state.session_id}] Starting review: {file_path} "
            f"(risk={risk_score:.2f})"
        )

        # Register finish_review as a special control tool
        self._register_finish_tool()

        # Build the first prompt
        messages = self.prompt.build_initial_prompt(state, self.tools)

        while state.current_step < AGENT["max_steps"]:
            # Periodic reflection every N steps
            if (
                state.current_step > 0
                and state.current_step % AGENT["reflection_interval"] == 0
            ):
                self._do_reflection(state)

            # Call the LLM
            try:
                response = self.llm.complete(messages)
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                state.status = AgentStatus.FAILED
                state.final_review = f"Review failed: LLM error: {e}"
                break

            # Parse thought / action / action_input
            parsed = self._parse_response(response, state)
            if parsed is None:
                if self._consecutive_parse_failures >= AGENT["max_retries"]:
                    logger.error(
                        f"[{state.session_id}] Parse failed "
                        f"{AGENT['max_retries']} times. Stopping."
                    )
                    state.status = AgentStatus.FAILED
                    break
                recovery_messages = self.prompt.build_parse_error_recovery_prompt(
                    response, "Could not extract Thought/Action/Action Input"
                )
                messages = recovery_messages
                continue

            self._consecutive_parse_failures = 0
            thought, action, action_input = parsed

            # Check for the terminal action
            if action == self.FINISH_TOOL:
                self._handle_finish(state, action_input)
                break

            # Execute the chosen tool
            logger.info(
                f"[{state.session_id}] Step {state.current_step}: "
                f"{action}({action_input})"
            )
            observation = self.tools.call(action, action_input)

            # Truncate very long observations to avoid context overflow
            if len(observation) > 3000:
                observation = observation[:3000] + "\n... [truncated]"

            # Auto-populate state.issues_found from tool output
            self._extract_issues_from_observation(action, observation, state)

            # Record the step and build the continuation prompt
            state.add_step(thought, action, action_input, observation)

            messages = self.prompt.build_continuation_prompt(state, observation)
            messages.append({
                "role": "user",
                "content": f"Observation: {observation}\n\nThought:"
            })

        else:
            # Exited while loop because max_steps was reached
            logger.warning(
                f"[{state.session_id}] Max steps ({AGENT['max_steps']}) reached"
            )
            state.status = AgentStatus.MAX_STEPS_REACHED
            state.final_review = self._generate_partial_review(state)

        state.completed_at = datetime.now()
        logger.success(
            f"[{state.session_id}] Review complete: "
            f"{len(state.issues_found)} issues, "
            f"{state.current_step} steps, "
            f"{state.elapsed_seconds:.1f}s"
        )
        return state

    # ── Private Methods ────────────────────────────────────────────────────────

    def _parse_response(
        self, response: str, state: AgentState
    ) -> Optional[tuple[str, str, dict]]:
        """Parse an LLM response into a (thought, action, action_input) triple.

        Expected format::

            Thought: <reasoning text>
            Action: <tool_name>
            Action Input: <json object>

        Handles: missing fields, single-quoted JSON, trailing commas in JSON,
        bare file paths as Action Input, and falls back gracefully on any error.

        Args:
            response: Raw text response from the LLM.
            state: Current AgentState (used for logging only).

        Returns:
            (thought, action, action_input) tuple, or None if parsing fails.
        """
        try:
            # Extract Thought
            thought_match = re.search(
                r"Thought:\s*(.+?)(?=\nAction:|$)", response, re.DOTALL
            )
            thought = thought_match.group(1).strip() if thought_match else ""

            # Extract Action (must be a word token)
            action_match = re.search(r"Action:\s*(\w+)", response)
            if not action_match:
                self._consecutive_parse_failures += 1
                logger.warning(
                    f"[{state.session_id}] Could not find Action in response"
                )
                return None
            action = action_match.group(1).strip()

            # Extract Action Input — look for a JSON object
            input_match = re.search(
                r"Action Input:\s*(\{.*?\})", response, re.DOTALL
            )
            if not input_match:
                # Fallback: try to grab whatever follows "Action Input:"
                fallback_match = re.search(
                    r"Action Input:\s*(.+?)(?=\n|$)", response
                )
                if fallback_match:
                    raw = fallback_match.group(1).strip()
                    # Bare .py path → wrap as file_path
                    if raw.endswith(".py"):
                        action_input: dict = {"file_path": raw}
                    else:
                        action_input = {"input": raw}
                else:
                    action_input = {}
            else:
                raw_json = input_match.group(1).strip()
                try:
                    action_input = json.loads(raw_json)
                except json.JSONDecodeError:
                    # Attempt common fixups: single quotes → double, trailing commas
                    fixed = raw_json.replace("'", '"')
                    fixed = re.sub(r",\s*}", "}", fixed)
                    fixed = re.sub(r",\s*]", "]", fixed)
                    try:
                        action_input = json.loads(fixed)
                    except json.JSONDecodeError:
                        self._consecutive_parse_failures += 1
                        logger.warning(
                            f"[{state.session_id}] JSON parse failed after fixup: "
                            f"{raw_json[:100]}"
                        )
                        return None

            return thought, action, action_input

        except Exception as e:
            logger.warning(f"[{state.session_id}] Parse error: {e}")
            self._consecutive_parse_failures += 1
            return None

    def _do_reflection(self, state: AgentState) -> None:
        """Run a reflection step — ask the agent to summarise and reprioritise.

        This is a fire-and-forget call: if the reflection LLM call fails for any
        reason the loop continues without interruption.

        Args:
            state: Current AgentState used to build the reflection prompt.
        """
        logger.info(
            f"[{state.session_id}] Reflection at step {state.current_step}"
        )
        try:
            reflection_messages = self.prompt.build_reflection_prompt(state)
            reflection = self.llm.complete(reflection_messages, max_tokens=300)
            logger.info(f"Reflection: {reflection[:200]}")
        except Exception as e:
            logger.warning(f"Reflection failed (non-fatal): {e}")

    def _handle_finish(self, state: AgentState, action_input: dict) -> None:
        """Process the finish_review action and mark the state as completed.

        Args:
            state: The AgentState to finalise.
            action_input: Dict that should contain 'detailed_review' and 'summary'.
        """
        state.final_review = action_input.get("detailed_review", "")
        state.summary = action_input.get("summary", "")
        state.status = AgentStatus.COMPLETED
        logger.success(f"[{state.session_id}] Agent finished review")

    def _generate_partial_review(self, state: AgentState) -> str:
        """Generate a best-effort review from partial findings when max steps is reached.

        Args:
            state: The AgentState with whatever findings were collected.

        Returns:
            Markdown-formatted partial review string.
        """
        issues_text = "\n".join(
            f"- [{i.severity.value}] {i.title}" for i in state.issues_found
        ) or "No issues identified before step limit reached."

        return (
            f"## Partial Review — {state.file_path}\n\n"
            f"**Note:** Review reached max steps ({AGENT['max_steps']}) "
            f"before completion.\n\n"
            f"**Issues found so far:**\n{issues_text}\n\n"
            f"**Steps taken:** {state.current_step}\n"
            f"**Tools used:** {', '.join(set(state.tools_called))}"
        )

    def _extract_issues_from_observation(
        self, action: str, observation: str, state: AgentState
    ) -> None:
        """Parse tool output and auto-populate state.issues_found.

        The LLM writes a great narrative review but never calls add_issue()
        directly — this method bridges that gap by parsing the structured
        output from ruff, bandit, and radon into ReviewIssue objects so
        Neo4j gets real data to store and pattern-detect against.

        Args:
            action: The tool name that produced this observation.
            observation: The raw string returned by the tool.
            state: AgentState to append ReviewIssue objects to.
        """
        if "ERROR" in observation or not observation.strip():
            return

        try:
            if action == "run_bandit":
                self._parse_bandit_issues(observation, state)
            elif action == "run_ruff":
                self._parse_ruff_issues(observation, state)
            elif action == "run_radon":
                self._parse_radon_issues(observation, state)
        except Exception as e:
            logger.debug(f"Issue extraction failed for {action} (non-fatal): {e}")

    def _parse_bandit_issues(self, observation: str, state: AgentState) -> None:
        """Extract ReviewIssue objects from a bandit observation string."""
        lines = observation.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            # Match: "  Line 17: [HIGH/HIGH] Issue text"
            if line.startswith("Line ") and "[" in line and "]" in line:
                try:
                    # Extract line number
                    line_num = int(line.split("Line ")[1].split(":")[0].strip())

                    # Extract severity (first of HIGH/MEDIUM/LOW pair)
                    bracket = line[line.index("[")+1:line.index("]")]
                    raw_sev = bracket.split("/")[0].strip().upper()
                    severity_map = {
                        "HIGH": Severity.HIGH,
                        "MEDIUM": Severity.MEDIUM,
                        "LOW": Severity.LOW,
                    }
                    severity = severity_map.get(raw_sev, Severity.LOW)

                    # Issue text follows the bracket
                    issue_text = line[line.index("]")+1:].strip()
                    if not issue_text:
                        i += 1
                        continue

                    # Peek at next line for test name
                    test_name = ""
                    if i + 1 < len(lines) and "Test:" in lines[i+1]:
                        test_name = lines[i+1].split("Test:")[1].split("—")[0].strip()

                    state.add_issue(ReviewIssue(
                        file_path=state.file_path,
                        line_number=line_num,
                        severity=severity,
                        category="security",
                        title=issue_text[:80],
                        description=issue_text,
                        suggestion=f"Fix {test_name} — see bandit docs for remediation.",
                        source_tool="bandit",
                        confidence=0.85,
                    ))
                except (ValueError, IndexError):
                    pass
            i += 1

    def _parse_ruff_issues(self, observation: str, state: AgentState) -> None:
        """Extract ReviewIssue objects from a ruff observation string."""
        # Severity mapping by ruff rule prefix
        severity_by_prefix = {
            "E": Severity.MEDIUM,   # pycodestyle errors
            "W": Severity.LOW,      # pycodestyle warnings
            "F": Severity.MEDIUM,   # pyflakes (unused imports, undefined names)
            "S": Severity.HIGH,     # flake8-bandit security
            "B": Severity.MEDIUM,   # bugbear
            "C": Severity.MEDIUM,   # complexity
            "N": Severity.LOW,      # naming
        }
        category_by_prefix = {
            "E": "style", "W": "style", "F": "style",
            "S": "security", "B": "logic",
            "C": "complexity", "N": "style",
        }

        for line in observation.splitlines():
            line = line.strip()
            # Match: "  Line 6: [F401] `os` imported but unused"
            if line.startswith("Line ") and "[" in line and "]" in line:
                try:
                    line_num = int(line.split("Line ")[1].split(":")[0].strip())
                    code = line[line.index("[")+1:line.index("]")].strip()
                    message = line[line.index("]")+1:].strip()
                    # Strip trailing URL in parentheses
                    if "  (" in message:
                        message = message[:message.rindex("  (")].strip()

                    prefix = code[0] if code else "E"
                    severity = severity_by_prefix.get(prefix, Severity.LOW)
                    category = category_by_prefix.get(prefix, "style")

                    if not message:
                        continue

                    state.add_issue(ReviewIssue(
                        file_path=state.file_path,
                        line_number=line_num,
                        severity=severity,
                        category=category,
                        title=f"[{code}] {message[:70]}",
                        description=message,
                        suggestion=f"Fix ruff rule {code}.",
                        source_tool="ruff",
                        confidence=0.90,
                    ))
                except (ValueError, IndexError):
                    pass

    def _parse_radon_issues(self, observation: str, state: AgentState) -> None:
        """Extract ReviewIssue objects from a radon observation string.

        Only flags functions graded C or worse (complexity ≥ 11).
        """
        for line in observation.splitlines():
            line = line.strip()
            # Match: "  compute_risk_score (line 44): complexity=25, grade=D ← COMPLEX"
            if "complexity=" in line and "grade=" in line:
                try:
                    func_name = line.split("(line")[0].strip()
                    line_num = int(line.split("(line")[1].split(")")[0].strip())
                    complexity = int(line.split("complexity=")[1].split(",")[0].strip())
                    grade = line.split("grade=")[1][0].upper()

                    if grade not in ("C", "D", "E", "F"):
                        continue

                    severity = Severity.HIGH if grade in ("E", "F") else Severity.MEDIUM
                    state.add_issue(ReviewIssue(
                        file_path=state.file_path,
                        line_number=line_num,
                        severity=severity,
                        category="complexity",
                        title=f"High complexity: {func_name}() grade={grade} (cc={complexity})",
                        description=(
                            f"Function '{func_name}' has cyclomatic complexity {complexity} "
                            f"(grade {grade}). Threshold for concern is C (cc≥11)."
                        ),
                        suggestion=(
                            f"Refactor '{func_name}' — extract sub-functions, "
                            "reduce branching, or apply early-return patterns."
                        ),
                        source_tool="radon",
                        confidence=0.95,
                    ))
                except (ValueError, IndexError):
                    pass

    def _register_finish_tool(self) -> None:
        """Register the finish_review control tool if it is not already registered.

        This tool is intentionally registered here (inside the loop engine) rather
        than in any tool file, keeping control flow separate from analysis tools.
        """
        if self.FINISH_TOOL not in self.tools:

            def finish_review(summary: str, detailed_review: str) -> str:
                """Signal that the review is complete.

                Args:
                    summary: One-paragraph summary of all findings.
                    detailed_review: Full markdown-formatted review report.

                Returns:
                    Confirmation string (result is ignored by the loop).
                """
                return "Review finalized."

            self.tools.register(Tool(
                name=self.FINISH_TOOL,
                description=(
                    "Call this when you have completed the review. "
                    "Provide a one-paragraph summary and a detailed markdown review."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "One paragraph summary of findings",
                        },
                        "detailed_review": {
                            "type": "string",
                            "description": "Full markdown review report",
                        },
                    },
                    "required": ["summary", "detailed_review"],
                },
                func=finish_review,
                category="control",
            ))