from typing import Any, Literal

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from tests.fakes import ScriptedLLM
from triage.config import Settings
from triage.llm import LLMConfigError, StructuredOutputError, call_structured, make_chat_model

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Verdict(BaseModel):
    label: Literal["yes", "no"]


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)


# --- provider factory ------------------------------------------------------------------


async def test_gemini_requires_model_and_key() -> None:
    with pytest.raises(LLMConfigError, match="LLM_MODEL, GEMINI_API_KEY"):
        make_chat_model(settings(llm_provider="gemini"))


async def test_gemini_model_is_configured() -> None:
    llm = make_chat_model(
        settings(llm_model="gemini-test-flash", gemini_api_key="k", llm_temperature=0.0)
    )
    assert type(llm).__name__ == "ChatGoogleGenerativeAI"
    assert llm.model.endswith("gemini-test-flash") and llm.temperature == 0.0


async def test_azure_lists_every_missing_setting() -> None:
    with pytest.raises(LLMConfigError) as exc:
        make_chat_model(settings(llm_provider="azure_openai"))
    for name in ("ENDPOINT", "API_KEY", "API_VERSION", "DEPLOYMENT"):
        assert f"AZURE_OPENAI_{name}" in str(exc.value)


async def test_azure_swap_is_config_only() -> None:
    llm = make_chat_model(
        settings(
            llm_provider="azure_openai",
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_api_key="k",
            azure_openai_api_version="2024-10-21",
            azure_openai_deployment="triage-gpt",
        )
    )
    assert type(llm).__name__ == "AzureChatOpenAI"
    assert llm.deployment_name == "triage-gpt"


async def test_retry_setting_means_retries_for_both_providers() -> None:
    # Google's SDK counts the first try as an attempt; OpenAI's doesn't.
    gemini = make_chat_model(settings(llm_model="m", gemini_api_key="k", llm_max_retries=3))
    azure = make_chat_model(
        settings(
            llm_provider="azure_openai",
            azure_openai_endpoint="https://example.openai.azure.com",
            azure_openai_api_key="k",
            azure_openai_api_version="2024-10-21",
            azure_openai_deployment="d",
            llm_max_retries=3,
        )
    )
    assert gemini.max_retries == 4
    assert azure.max_retries == 3


# --- call_structured ---------------------------------------------------------------------


async def test_returns_value_usage_and_latency() -> None:
    result = await call_structured(ScriptedLLM({"label": "yes"}), Verdict, [HumanMessage("?")])
    assert result.value == Verdict(label="yes")
    assert (result.usage.input_tokens, result.usage.total_tokens) == (100, 120)
    assert result.attempts == 1 and result.latency_ms >= 0


class FlakyLLM:
    """Invalid output on the first call, valid on the second."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.histories: list[list] = []

    def with_structured_output(self, schema: type[BaseModel], include_raw: bool = False, **_: Any):
        async def run(messages: list) -> dict:
            self.histories.append(list(messages))
            raw = AIMessage(
                content="{}",
                usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            )
            if len(self.histories) <= self.fail_times:
                return {"raw": raw, "parsed": None, "parsing_error": ValueError("label missing")}
            return {"raw": raw, "parsed": schema(label="no"), "parsing_error": None}

        return RunnableLambda(run)


async def test_repairs_once_with_the_error_fed_back() -> None:
    llm = FlakyLLM(fail_times=1)
    result = await call_structured(llm, Verdict, [HumanMessage("?")])
    assert result.value.label == "no" and result.attempts == 2
    assert result.usage.total_tokens == 24  # both attempts are billed
    assert "label missing" in str(llm.histories[1][-1].content)


async def test_fails_closed_after_repair_attempt() -> None:
    with pytest.raises(StructuredOutputError, match="Verdict"):
        await call_structured(FlakyLLM(fail_times=5), Verdict, [HumanMessage("?")])


# --- transient provider errors ------------------------------------------------------------


def test_provider_outages_are_transient_and_everything_else_is_not() -> None:
    from langchain_core.exceptions import (
        ModelAPIError,
        ModelAuthenticationError,
        ModelRateLimitError,
    )

    from triage.llm import is_transient

    assert is_transient(ModelAPIError("503 UNAVAILABLE"))
    try:  # wrapped by another layer: the cause still counts
        try:
            raise ModelRateLimitError("429")
        except ModelRateLimitError as inner:
            raise RuntimeError("node failed") from inner
    except RuntimeError as outer:
        assert is_transient(outer)
    assert not is_transient(ModelAuthenticationError("401: bad key"))  # retrying won't help
    assert not is_transient(StructuredOutputError("still invalid"))
    assert not is_transient(ValueError("a bug"))
