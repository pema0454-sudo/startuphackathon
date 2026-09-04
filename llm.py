"""
llm.py — thin wrapper around the hackathon's OpenAI-compatible model API.

  base_url: https://hackathon-2026-2-resource.openai.azure.com/openai/v1/
  api_key:  HACKATHON_KEY (from .env)

Model routing (spec §11):
  - DeepSeek-V4-Flash  -> cheap/fast: parsing a raw station feed string
  - gpt-5.5            -> the one planning + claim-drafting call per run

Every call is wrapped in exponential backoff with jitter, a hard wall-clock
timeout, and a running token/cost counter that gets written to the trace.
If no key is configured, or every retry fails, callers get an
LLMUnavailable exception and are expected to fall back to DEMO AI MODE
(agent.py handles that — this module never invents a fake answer).
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

DEEPSEEK_MODEL = "DeepSeek-V4-Flash"
GPT_MODEL = "gpt-5.5"

HACKATHON_BASE_URL = os.getenv(
    "HACKATHON_BASE_URL",
    "https://hackathon-2026-2-resource.openai.azure.com/openai/v1/",
)
HACKATHON_KEY = os.getenv("HACKATHON_KEY", "").strip()

MAX_RETRIES = 3
BASE_BACKOFF = 1.5  # seconds
CALL_TIMEOUT = 20  # seconds, per HTTP request

# Very rough NPR-per-1k-token estimate used only for the trace's cost line —
# not a billing figure, just a fair-use visibility aid.
NPR_PER_1K_TOKENS = {"DeepSeek-V4-Flash": 0.4, "gpt-5.5": 2.1}


class LLMUnavailable(Exception):
    """Raised when the hackathon model API can't be reached after retries,
    or no API key is configured at all. Callers must fall back to
    DEMO AI MODE rather than block or crash."""


@dataclass
class UsageTracker:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_npr: float = 0.0
    _cache: dict = field(default_factory=dict)

    def record(self, model: str, usage) -> None:
        self.calls += 1
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        self.prompt_tokens += pt
        self.completion_tokens += ct
        rate = NPR_PER_1K_TOKENS.get(model, 1.0)
        self.estimated_cost_npr += (pt + ct) / 1000 * rate

    def summary(self) -> str:
        return (
            f"{self.calls} calls · {self.prompt_tokens + self.completion_tokens} tokens "
            f"· ~NPR {self.estimated_cost_npr:.2f}"
        )


_client = None


def _get_client():
    global _client
    if _client is None:
        if not HACKATHON_KEY:
            raise LLMUnavailable("HACKATHON_KEY not set")
        from openai import OpenAI

        _client = OpenAI(base_url=HACKATHON_BASE_URL, api_key=HACKATHON_KEY, timeout=CALL_TIMEOUT)
    return _client


def _with_retries(fn, *, on_retry=None):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - any transport/API error is transient here
            last_err = e
            if attempt == MAX_RETRIES:
                break
            backoff = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            if on_retry:
                on_retry(attempt, backoff, e)
            time.sleep(backoff)
    raise LLMUnavailable(f"model API failed after {MAX_RETRIES} attempts: {last_err}")


def chat(model: str, messages: list, usage: UsageTracker, tools: list | None = None,
         tool_choice: str | None = None, on_retry=None):
    """Single chat-completion call with retries. Returns the raw message
    object from choices[0].message."""
    client = _get_client()

    # Fair-use: don't re-send an identical (model, messages) pair twice in
    # the same run — serve the cached completion instead.
    cache_key = (model, tuple((m.get("role"), str(m.get("content"))) for m in messages))
    if cache_key in usage._cache:
        return usage._cache[cache_key]

    def _call():
        kwargs = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"
        return client.chat.completions.create(**kwargs)

    resp = _with_retries(_call, on_retry=on_retry)
    usage.record(model, getattr(resp, "usage", None))
    msg = resp.choices[0].message
    usage._cache[cache_key] = msg
    return msg


def parse_reading_text(raw_text: str, usage: UsageTracker, on_retry=None) -> dict | None:
    """Use the cheap/fast model to parse a messy station-feed line into a
    structured reading. Returns None (never raises) so callers can fall
    back to a regex parse — this is a nice-to-have, not load-bearing."""
    try:
        msg = chat(
            DEEPSEEK_MODEL,
            [
                {
                    "role": "system",
                    "content": (
                        "Extract station telemetry from the text as compact JSON with keys "
                        "value (number, metres), trend (one of rising/falling/steady). "
                        "Reply with JSON only, no prose."
                    ),
                },
                {"role": "user", "content": raw_text},
            ],
            usage,
            on_retry=on_retry,
        )
        import json as _json

        return _json.loads(msg.content)
    except Exception:  # noqa: BLE001
        return None
