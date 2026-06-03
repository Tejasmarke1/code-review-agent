"""
Groq LLM Client
================
Thin wrapper around the Groq API.
Handles: retries, timeouts, rate limits, response parsing.

Never import this directly in agent code — use via prompt_engine.py.
"""

import time
from typing import Optional
from openai import OpenAI  # Groq uses OpenAI-compatible API
from loguru import logger
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import LLM


class GroqClient:
    """
    OpenAI-compatible client pointed at Groq's API.

    Groq is OpenAI API-compatible so we use the openai SDK
    with a custom base_url. This is the standard pattern.
    """

    def __init__(self):
        """Initialise the Groq client.

        Raises:
            ValueError: If GROQ_API_KEY is not set in the environment.
        """
        if not LLM["api_key"]:
            raise ValueError(
                "GROQ_API_KEY not set. Get a free key at https://console.groq.com"
            )
        self.client = OpenAI(
            api_key=LLM["api_key"],
            base_url=LLM["api_base"],
            timeout=LLM["timeout"],
        )
        self.model = LLM["model"]
        self._call_count = 0
        self._total_tokens = 0

    def complete(
        self,
        messages: list[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send messages to Groq and return the text response.

        Retries on rate limit (429) with exponential backoff.
        Raises on all other errors after logging.

        Args:
            messages: OpenAI-format message list, e.g.
                      [{"role": "user", "content": "Hello"}]
            temperature: Override the default temperature if provided.
            max_tokens: Override the default max_tokens if provided.

        Returns:
            Raw text response string from the model.

        Raises:
            RuntimeError: If the API continues to rate-limit after 3 retries.
        """
        temp = temperature if temperature is not None else LLM["temperature"]
        tokens = max_tokens if max_tokens is not None else LLM["max_tokens"]

        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=tokens,
                )
                self._call_count += 1
                self._total_tokens += response.usage.total_tokens if response.usage else 0

                content = response.choices[0].message.content
                logger.debug(f"Groq response [{self._call_count}]: {len(content)} chars")
                return content

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "rate_limit" in err_str.lower():
                    wait = 2 ** attempt
                    logger.warning(f"Rate limited. Waiting {wait}s (attempt {attempt+1}/3)")
                    time.sleep(wait)
                    continue
                logger.error(f"Groq API error: {e}")
                raise

        raise RuntimeError("Groq API failed after 3 retries (rate limit)")

    def get_usage_stats(self) -> dict:
        """Return a snapshot of token usage and estimated cost.

        Returns:
            Dict with keys: total_calls, total_tokens, estimated_cost_usd.
        """
        return {
            "total_calls": self._call_count,
            "total_tokens": self._total_tokens,
            "estimated_cost_usd": 0.0,  # Groq free tier
        }