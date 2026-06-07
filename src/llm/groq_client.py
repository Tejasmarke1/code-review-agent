"""
Groq LLM Client
================
Thin wrapper around the Groq API.
"""

import time
from collections import deque
from typing import Optional

from loguru import logger
from openai import OpenAI

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from configs.config import LLM


class GroqClient:
    """
    OpenAI-compatible client pointed at Groq's API with token-aware
    rate limiting to handle the free-tier tokens-per-minute constraint.
    """

    TOKEN_LIMIT_PER_MINUTE = 10000

    def __init__(self):
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
        self._last_call_time: float = 0.0
        self._min_interval: float = 2.0
        self._token_window: deque = deque()  # (timestamp, tokens) pairs

    def _wait_for_token_budget(self, estimated_tokens: int = 500) -> None:
        """Sleep if the rolling 60-second token window is close to the limit."""
        now = time.time()
        while self._token_window and now - self._token_window[0][0] > 60:
            self._token_window.popleft()

        tokens_used = sum(t for _, t in self._token_window)

        if tokens_used + estimated_tokens > self.TOKEN_LIMIT_PER_MINUTE:
            if self._token_window:
                oldest_time = self._token_window[0][0]
                wait = 60 - (now - oldest_time) + 1.0
                logger.warning(
                    f"Token budget: {tokens_used}/{self.TOKEN_LIMIT_PER_MINUTE} used. "
                    f"Waiting {wait:.1f}s for window to reset"
                )
                time.sleep(max(0.0, wait))
            else:
                logger.warning("Token window empty but limit reached — waiting 60s")
                time.sleep(60)

    def complete(
        self,
        messages: list[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Send messages to Groq and return the text response."""
        temp = temperature if temperature is not None else LLM["temperature"]
        tokens = max_tokens if max_tokens is not None else LLM["max_tokens"]

        elapsed = time.time() - self._last_call_time
        if elapsed < self._min_interval:
            sleep_for = self._min_interval - elapsed
            logger.debug(f"Throttle: sleeping {sleep_for:.2f}s before Groq call")
            time.sleep(sleep_for)

        self._wait_for_token_budget(estimated_tokens=tokens or 500)

        retry_waits = [4, 8, 16, 32, 60]
        for attempt in range(5):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temp,
                    max_tokens=tokens,
                )
                actual_tokens = response.usage.total_tokens if response.usage else 500
                self._token_window.append((time.time(), actual_tokens))
                self._last_call_time = time.time()
                self._call_count += 1
                self._total_tokens += actual_tokens

                content = response.choices[0].message.content
                logger.debug(
                    f"Groq response [{self._call_count}]: "
                    f"{len(content)} chars, {actual_tokens} tokens"
                )
                return content

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "rate_limit" in err_str.lower():
                    wait = retry_waits[attempt] if attempt < len(retry_waits) else 60
                    logger.warning(f"Rate limited. Waiting {wait}s (attempt {attempt+1}/5)")
                    time.sleep(wait)
                    continue
                logger.error(f"Groq API error: {e}")
                raise

        raise RuntimeError("Groq API failed after 5 retries (rate limit)")

    def get_usage_stats(self) -> dict:
        """Return token usage and call count statistics."""
        return {
            "total_calls": self._call_count,
            "total_tokens": self._total_tokens,
            "estimated_cost_usd": 0.0,
        }