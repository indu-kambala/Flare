"""Anthropic API client with retry logic and rate limiting."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Generic, TypeVar

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from flare.llm.schemas import UsageStats

T = TypeVar("T", bound=BaseModel)

load_dotenv()

logger = logging.getLogger(__name__)

# Claude Sonnet 5 standard pricing (per million tokens).
# Introductory pricing ($2.00 / $10.00) runs through 2026-08-31, so these
# constants over-estimate cost until then rather than under-report it.
_INPUT_COST_PER_M = 3.0
_OUTPUT_COST_PER_M = 15.0

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_TIMEOUT = 60.0  # seconds per API call


@dataclass
class LLMResponse(Generic[T]):
    """A schema-validated response from the Anthropic API.

    Attributes:
        parsed: The response validated against the requested Pydantic schema.
        content: ``parsed`` as a plain JSON-compatible dict.
        raw_text: Raw text of the response.
        usage: Token usage and cost statistics.
    """

    parsed: T
    content: dict
    raw_text: str
    usage: UsageStats


class AnthropicClient:
    """Client for the Anthropic API with retry logic and cost tracking.

    Loads ANTHROPIC_API_KEY from environment variables. Implements
    exponential backoff for transient failures and rate limit errors.

    Example:
        >>> client = AnthropicClient()
        >>> response = client.complete(
        ...     system=SUMMARIZE_SYSTEM_PROMPT, user=prompt, schema=LLMSummary
        ... )
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        base_delay: float = DEFAULT_BASE_DELAY,
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
    ) -> None:
        """Initialize the client.

        Args:
            model: Anthropic model ID to use.
            max_tokens: Maximum tokens in the response.
            max_retries: Maximum number of retry attempts.
            base_delay: Base delay in seconds for exponential backoff.
            timeout: Per-call timeout in seconds. Raises APITimeoutError if exceeded.
            api_key: API key. If None, reads from ANTHROPIC_API_KEY env var.

        Raises:
            ValueError: If no API key is found.
        """
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.timeout = timeout

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not resolved_key:
            raise ValueError(
                "ANTHROPIC_API_KEY not found. Set it in your environment or .env file."
            )

        self._client = anthropic.Anthropic(api_key=resolved_key)

    def complete(self, system: str, user: str, schema: type[T]) -> LLMResponse[T]:
        """Send a completion request with retry logic, constrained to a schema.

        Uses structured outputs: ``schema`` is sent as a JSON schema the API
        enforces server-side, so the response is guaranteed to parse and
        validate. This replaces both the sampling controls the Messages API no
        longer exposes (``temperature`` and friends) and the markdown-fence
        stripping this client used to need.

        Args:
            system: System prompt.
            user: User message.
            schema: Pydantic model the response must conform to.

        Returns:
            LLMResponse carrying the validated ``schema`` instance.

        Raises:
            anthropic.APIError: If all retries are exhausted.
        """
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            try:
                start_time = time.monotonic()

                response = self._client.messages.parse(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_format=schema,
                    timeout=self.timeout,
                )

                latency_ms = (time.monotonic() - start_time) * 1000

                first_block = response.content[0]
                raw_text = first_block.text if hasattr(first_block, "text") else str(first_block)
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens

                usage = UsageStats(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=round(latency_ms, 1),
                    estimated_cost_usd=self._estimate_cost(
                        input_tokens, output_tokens
                    ),
                )

                parsed = response.parsed_output
                if parsed is None:
                    # No schema-valid output: a refusal, or max_tokens truncation.
                    # Both repeat on retry, so fail loudly instead of burning attempts.
                    raise ValueError(
                        "Model returned no schema-valid output "
                        f"(stop_reason={response.stop_reason!r}, "
                        f"schema={schema.__name__}): {raw_text[:200]!r}"
                    )

                return LLMResponse(
                    parsed=parsed,
                    content=parsed.model_dump(mode="json"),
                    raw_text=raw_text,
                    usage=usage,
                )

            except anthropic.APITimeoutError as e:
                last_error = e
                delay = self.base_delay * (2**attempt)
                logger.warning(
                    "Timeout after %.1fs (attempt %d/%d), retrying in %.1fs",
                    self.timeout,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)

            except anthropic.RateLimitError as e:
                last_error = e
                delay = self.base_delay * (2**attempt)
                logger.warning(
                    "Rate limited (attempt %d/%d), retrying in %.1fs",
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)

            except anthropic.APIStatusError as e:
                if e.status_code >= 500:
                    last_error = e
                    delay = self.base_delay * (2**attempt)
                    logger.warning(
                        "Server error %d (attempt %d/%d), retrying in %.1fs",
                        e.status_code,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    raise

        raise last_error  # type: ignore[misc]

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate USD cost based on token counts."""
        input_cost = (input_tokens / 1_000_000) * _INPUT_COST_PER_M
        output_cost = (output_tokens / 1_000_000) * _OUTPUT_COST_PER_M
        return round(input_cost + output_cost, 6)
