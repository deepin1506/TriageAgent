"""LLM layer: provider swap via config, plus one helper for every structured LLM call.

Nodes should call `call_structured(...)` instead of `llm.with_structured_output(...)`
directly, so every call gets the same validation, repair retry and usage accounting
(Phase 6 writes `StructuredResult.usage` / `latency_ms` to the run log).
"""

import time
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel

from triage.config import Settings


class LLMConfigError(RuntimeError):
    """Missing or invalid LLM settings. Raised at startup, before any ticket is touched."""


class StructuredOutputError(RuntimeError):
    """The model did not produce a valid object after the repair attempt (fail closed)."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.total_tokens + other.total_tokens,
        )


@dataclass
class StructuredResult[T: BaseModel]:
    value: T
    usage: Usage
    latency_ms: float
    attempts: int


def make_chat_model(settings: Settings) -> BaseChatModel:
    """Build the chat model for the configured provider. No network call happens here."""
    common: dict[str, Any] = {
        "timeout": settings.llm_timeout_s,
        "max_retries": settings.llm_max_retries,
    }
    if settings.llm_temperature is not None:
        common["temperature"] = settings.llm_temperature

    if settings.llm_provider == "gemini":
        missing = [
            name
            for name, value in (
                ("LLM_MODEL", settings.llm_model),
                ("GEMINI_API_KEY", settings.gemini_api_key),
            )
            if not value
        ]
        if missing:
            raise LLMConfigError(f"Gemini needs {', '.join(missing)} in .env")
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.llm_model,
            api_key=settings.gemini_api_key.get_secret_value(),  # type: ignore[union-attr]
            **common,
        )

    required = {
        "AZURE_OPENAI_ENDPOINT": settings.azure_openai_endpoint,
        "AZURE_OPENAI_API_KEY": settings.azure_openai_api_key,
        "AZURE_OPENAI_API_VERSION": settings.azure_openai_api_version,
        "AZURE_OPENAI_DEPLOYMENT": settings.azure_openai_deployment,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise LLMConfigError(f"Azure OpenAI needs {', '.join(missing)} in .env")
    from langchain_openai import AzureChatOpenAI

    return AzureChatOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key.get_secret_value(),  # type: ignore[union-attr]
        api_version=settings.azure_openai_api_version,
        azure_deployment=settings.azure_openai_deployment,
        **common,
    )


def _usage(message: Any) -> Usage:
    meta = getattr(message, "usage_metadata", None) or {}
    return Usage(
        input_tokens=meta.get("input_tokens", 0),
        output_tokens=meta.get("output_tokens", 0),
        total_tokens=meta.get("total_tokens", 0),
    )


async def call_structured[T: BaseModel](
    llm: BaseChatModel,
    schema: type[T],
    messages: list[BaseMessage],
    *,
    max_attempts: int = 2,
) -> StructuredResult[T]:
    """Ask the model for a `schema` instance.

    On a parsing/validation failure, the error is sent back once so the model can repair
    its output. If it still fails, raise StructuredOutputError: callers fail closed.
    """
    runnable = llm.with_structured_output(schema, include_raw=True)
    history = list(messages)
    usage = Usage()
    start = time.perf_counter()
    last_error: Any = None

    for attempt in range(1, max_attempts + 1):
        out = await runnable.ainvoke(history)
        usage = usage + _usage(out.get("raw"))
        parsed, last_error = out.get("parsed"), out.get("parsing_error")
        if isinstance(parsed, schema) and last_error is None:
            latency = (time.perf_counter() - start) * 1000
            return StructuredResult(parsed, usage, latency, attempt)
        raw = out.get("raw")
        history += [
            # Content only: replaying provider tool_calls without tool results breaks some APIs.
            AIMessage(content=raw.content if isinstance(raw, AIMessage) else str(raw)),
            HumanMessage(
                content=(
                    f"Your previous output was not a valid {schema.__name__}: {last_error}. "
                    "Reply again with only a valid object."
                )
            ),
        ]
    raise StructuredOutputError(
        f"{schema.__name__} still invalid after {max_attempts} attempts: {last_error}"
    )
