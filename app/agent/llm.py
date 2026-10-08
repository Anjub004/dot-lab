"""LLM client abstraction.

The agent only depends on :class:`LLMClient`, a tiny protocol with one method
that returns a validated Pydantic object. :class:`OpenAILLM` implements it with
the official OpenAI Python SDK's Responses API and Structured Outputs
(``client.responses.parse(..., text_format=Model)``), so the model's output is
schema-constrained and parsed by the SDK — no fragile string parsing.

Tests use a scripted fake that implements the same protocol, so the test suite
never needs an API key or network access.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

import httpx
import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMError(RuntimeError):
    """The LLM call failed or returned unusable output."""


class LLMConfigurationError(LLMError):
    """The LLM is not configured (missing key or model)."""


class LLMClient(Protocol):
    """Anything that can turn a prompt into a validated Pydantic object."""

    @property
    def is_configured(self) -> bool: ...

    async def generate(self, *, system: str, prompt: str, schema: type[SchemaT]) -> SchemaT: ...


class UnconfiguredLLM:
    """Placeholder used when OPENAI_API_KEY / OPENAI_MODEL are missing."""

    MESSAGE = (
        "OpenAI is not configured. Set OPENAI_API_KEY and OPENAI_MODEL in your .env "
        "file (see .env.example) and restart Dot Lab."
    )

    @property
    def is_configured(self) -> bool:
        return False

    async def generate(self, *, system: str, prompt: str, schema: type[SchemaT]) -> SchemaT:
        raise LLMConfigurationError(self.MESSAGE)


class OpenAILLM:
    """OpenAI implementation using the Responses API with Structured Outputs."""

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        max_retries: int = 2,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not model:
            raise LLMConfigurationError(UnconfiguredLLM.MESSAGE)
        self.model = model
        # The SDK retries connection errors, 408/409/429 and 5xx with backoff.
        # ``http_client`` lets tests plug in a mock transport.
        self._client = AsyncOpenAI(
            api_key=api_key, timeout=timeout, max_retries=max_retries, http_client=http_client
        )

    @property
    def is_configured(self) -> bool:
        return True

    async def generate(self, *, system: str, prompt: str, schema: type[SchemaT]) -> SchemaT:
        try:
            response = await self._client.responses.parse(
                model=self.model,
                instructions=system,
                input=prompt,
                text_format=schema,
            )
        except openai.AuthenticationError as exc:
            raise LLMConfigurationError(
                "OpenAI rejected the API key (authentication error)"
            ) from exc
        except openai.NotFoundError as exc:
            raise LLMConfigurationError(
                f"OpenAI model '{self.model}' was not found. Check OPENAI_MODEL."
            ) from exc
        except openai.BadRequestError as exc:
            raise LLMError(f"OpenAI rejected the request: {exc.message}") from exc
        except openai.APITimeoutError as exc:
            raise LLMError("OpenAI request timed out") from exc
        except openai.APIError as exc:
            raise LLMError(f"OpenAI API error: {type(exc).__name__}") from exc
        except ValidationError as exc:
            raise LLMError("Model output did not match the expected schema") from exc

        parsed = response.output_parsed
        if parsed is None:
            raise LLMError("Model returned no structured output (possibly a refusal)")
        return parsed


def build_llm(
    api_key: str | None, model: str | None, timeout: float, max_retries: int
) -> LLMClient:
    """Return a real client when configured, else a placeholder that explains what's missing."""
    if api_key and model:
        return OpenAILLM(api_key, model, timeout=timeout, max_retries=max_retries)
    return UnconfiguredLLM()
