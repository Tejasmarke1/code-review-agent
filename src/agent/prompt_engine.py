"""
Prompt Engine
=============
Constructs all prompts sent to the LLM.
All prompt text lives here — never scattered through agent code.

Design principle: prompts are functions, not strings.
Every prompt function takes structured inputs and returns
a messages list ready for GroqClient.complete().
"""

from src.agent.state import AgentState
from src.tools.registry import ToolRegistry


class PromptEngine:
    """Constructs every prompt variant sent to the LLM during a review session."""

    def build_system_prompt(self) -> str:
        """Return the core system prompt that defines agent behaviour.

        Instructs the agent to follow the strict ReAct format and sets
        all review heuristics (when to use each tool, severity definitions, etc.).

        Returns:
            System prompt string.
        """
        return """You are an expert code reviewer agent. You autonomously review Python code by using tools to gather evidence, then synthesize findings into actionable feedback.

You MUST follow this exact format for every response:

Thought: [Your reasoning about what to do next. Be specific about what you suspect and why.]
Action: [EXACTLY one tool name from the available tools]
Action Input: [Valid JSON object with the tool's required parameters]

After receiving an Observation, continue with another Thought/Action/Action Input cycle.

When you have gathered enough evidence to write a complete review, use:
Action: finish_review
Action Input: {"summary": "one paragraph summary", "detailed_review": "full markdown review"}

Rules:
- Always start by reading the file if you haven't already
- Always run at least run_ruff AND run_bandit before finishing (exact tool names)
- Check complexity (radon) for any file over 100 lines
- Cross-reference with ML risk score: if risk is HIGH, investigate more deeply
- Never hallucinate issues — only report what tools confirm or what you can directly see in code
- If past issues exist in memory, explicitly reference them in your review
- Be specific: always include line numbers when possible
- Severity levels: CRITICAL (security/data loss), HIGH (bugs/crashes), MEDIUM (maintainability), LOW (style)
-Action Input MUST contain ONLY the exact parameters the tool requires (e.g. file_path).
  NEVER include file contents, code snippets, or any text in Action Input.
  Wrong:  Action Input: {"code": "def foo(): ..."}
  Right:  Action Input: {"file_path": "/path/to/file.py"}"""

    def build_initial_prompt(
        self,
        state: AgentState,
        tool_registry: ToolRegistry,
    ) -> list[dict]:
        """Build the first message list for a new file review.

        Includes: file context, ML risk score, SHAP features, available tools,
        and any past memory context retrieved from Neo4j.

        Args:
            state: The current AgentState for this review session.
            tool_registry: The ToolRegistry whose tools will be described.

        Returns:
            OpenAI-format messages list ready for GroqClient.complete().
        """
        tools_description = tool_registry.get_tools_description()

        shap_context = ""
        if state.shap_features:
            top_features = state.shap_features[:3]
            shap_context = (
                f"\nML Model Risk Analysis:\n"
                f"- Risk Score: {state.risk_score:.3f} ({state.risk_label})\n"
                f"- Top risk factors identified by the ML model:\n"
                + "\n".join(
                    f"  • {f['feature_name']}: {f['feature_value']} "
                    f"(SHAP: {f['shap_value']:+.3f})"
                    for f in top_features
                )
                + "\nUse these as starting hypotheses — the model flagged this file "
                "for specific reasons."
            )

        memory_context = ""
        if state.past_issues_context:
            memory_context = (
                f"\nPast Review Memory (from Neo4j):\n"
                f"{state.past_issues_context}\n"
                "Reference these patterns in your review if relevant."
            )

        user_message = (
            f"Review the following Python file for defects, security issues, "
            f"and code quality problems.\n\n"
            f"File: {state.file_path}\n"
            f"Repository: {state.repo_url}\n"
            f"{shap_context}\n"
            f"{memory_context}\n\n"
            f"Available Tools:\n{tools_description}\n\n"
            f"Begin your review. Start by reading the file.\n\n"
            f"Thought:"
        )

        return [
            {"role": "system", "content": self.build_system_prompt()},
            {"role": "user", "content": user_message},
        ]

    def build_continuation_prompt(
        self,
        state: AgentState,
        last_observation: str,
    ) -> list[dict]:
        """Build the full conversation history for continuing the ReAct loop.

        Reconstructs every Thought/Action/Observation turn from state so the
        LLM has complete context when producing its next action.

        Args:
            state: The current AgentState containing all previous thought steps.
            last_observation: The most recent tool observation (appended by caller).

        Returns:
            OpenAI-format messages list representing the full conversation so far.
        """
        messages: list[dict] = [
            {"role": "system", "content": self.build_system_prompt()},
        ]

        for i, step in enumerate(state.thought_history):
            if i == 0:
                messages.append({
                    "role": "user",
                    "content": f"Review file: {state.file_path}\n\nThought:"
                })

            # Assistant turn: thought + action
            messages.append({
                "role": "assistant",
                "content": (
                    f"Thought: {step.thought}\n"
                    f"Action: {step.action}\n"
                    f"Action Input: {step.action_input}"
                )
            })

            # User turn: observation
            tools_already_called = [step.action for step in state.thought_history]

            messages.append({
                "role": "user",
                "content": (
                    f"Observation: {last_observation}\n\n"
                    f"Tools already used this session: {', '.join(tools_already_called)}\n"
                    f"File being reviewed: {state.file_path}\n"
                    f"ALWAYS use the full absolute path shown above for file_path arguments.\n"
                    f"Do NOT repeat a tool you have already used unless the result was an error.\n\n"
                    f"Thought:"
                )
            })

        return messages

    def build_reflection_prompt(self, state: AgentState) -> list[dict]:
        """Build a periodic reflection prompt asking the agent to reprioritise.

        Called every AGENT['reflection_interval'] steps.  Asks the agent to
        summarise what it has found so far and identify the most important
        remaining check.

        Args:
            state: The current AgentState with all findings so far.

        Returns:
            OpenAI-format messages list for the reflection call.
        """
        issues_so_far = "\n".join(
            f"- [{i.severity.value}] {i.title} (line {i.line_number})"
            for i in state.issues_found
        ) or "None yet"

        tools_used = ", ".join(set(state.tools_called)) or "None yet"

        return [
            {"role": "system", "content": self.build_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"Reflection checkpoint (step {state.current_step}):\n\n"
                    f"File being reviewed: {state.file_path}\n"
                    f"Risk score: {state.risk_score:.3f} ({state.risk_label})\n"
                    f"Tools used so far: {tools_used}\n"
                    f"Issues found so far:\n{issues_so_far}\n\n"
                    f"What have you found so far? What is the most important thing "
                    f"left to check?\nBe concise — 2-3 sentences max.\n\nThought:"
                ),
            },
        ]

    def build_parse_error_recovery_prompt(
        self,
        bad_response: str,
        error_message: str,
    ) -> list[dict]:
        """Build a recovery prompt when the LLM produces malformed output.

        Shows the bad response and the parse error, then asks the model to
        retry with the exact expected format.

        Args:
            bad_response: The raw LLM response that could not be parsed.
            error_message: A short description of what went wrong during parsing.

        Returns:
            OpenAI-format messages list for the recovery call.
        """
        return [
            {"role": "system", "content": self.build_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"Your last response could not be parsed.\n\n"
                    f"Error: {error_message}\n\n"
                    f"Your response was:\n{bad_response}\n\n"
                    f"Please respond again following EXACTLY this format:\n"
                    f'Thought: [your reasoning]\n'
                    f'Action: [tool name]\n'
                    f'Action Input: {{"key": "value"}}\n\n'
                    f"Try again:"
                ),
            },
        ]