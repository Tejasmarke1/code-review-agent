"""
Tool Registry
=============
Central registry of all tools available to the agent.
Tools register themselves with a name, description, and callable.

Design: the agent sees only tool names and descriptions.
The registry handles routing, error catching, and timeout.
"""

import json
from dataclasses import dataclass
from typing import Any, Callable
from loguru import logger
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import AGENT


@dataclass
class Tool:
    """Descriptor for a single callable tool available to the agent."""
    name: str               # exact name agent must use in Action field
    description: str        # shown to agent in prompt
    parameters: dict        # JSON schema of expected parameters
    func: Callable          # the actual function to call
    category: str           # "file" / "analysis" / "memory" / "control"


class ToolRegistry:
    """
    Manages all tools available to the agent.

    Tools are registered at startup and never change during a session.
    The agent selects tools by name — the registry dispatches the call.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._call_counts: dict[str, int] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool with the registry.

        Args:
            tool: The Tool instance to register.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool
        self._call_counts[tool.name] = 0
        logger.debug(f"Registered tool: {tool.name}")

    def call(self, tool_name: str, tool_input: dict) -> str:
        """Call a tool by name with the given input dictionary.

        All errors are caught and returned as descriptive strings so that
        the agent loop is never interrupted by a tool exception.

        Args:
            tool_name: The exact registered name of the tool to invoke.
            tool_input: A dict of keyword arguments to pass to the tool function.

        Returns:
            String observation — always a string, never raises to the caller.
        """
        if tool_name not in self._tools:
            available = ", ".join(self._tools.keys())
            return f"ERROR: Unknown tool '{tool_name}'. Available tools: {available}"

        tool = self._tools[tool_name]
        self._call_counts[tool_name] += 1

        try:
            # Validate required parameters are present
            required = [
                k for k, v in tool.parameters.get("properties", {}).items()
                if k in tool.parameters.get("required", [])
            ]
            missing = [r for r in required if r not in tool_input]
            if missing:
                return (
                    f"ERROR: Missing required parameters: {missing}. "
                    f"Tool expects: {tool.parameters}"
                )

            result = tool.func(**tool_input)
            return str(result) if result is not None else "Tool completed with no output."

        except TypeError as e:
            return f"ERROR: Wrong parameters for tool '{tool_name}': {e}"
        except Exception as e:
            logger.error(f"Tool '{tool_name}' failed: {e}")
            return f"ERROR: Tool '{tool_name}' failed: {type(e).__name__}: {str(e)[:200]}"

    def get_tools_description(self) -> str:
        """Format all registered tools for inclusion in the agent prompt.

        Returns:
            A multi-line string listing each tool's name, description, and parameters.
        """
        lines = []
        for tool in self._tools.values():
            params = json.dumps(tool.parameters.get("properties", {}), indent=2)
            lines.append(
                f"Tool: {tool.name}\n"
                f"Description: {tool.description}\n"
                f"Parameters: {params}\n"
            )
        return "\n".join(lines)

    def get_call_stats(self) -> dict[str, int]:
        """Return a dict mapping each tool name to the number of times it was called.

        Returns:
            Dict of {tool_name: call_count}.
        """
        return dict(self._call_counts)

    def list_tool_names(self) -> list[str]:
        """Return a list of all registered tool names.

        Returns:
            List of tool name strings.
        """
        return list(self._tools.keys())

    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self._tools