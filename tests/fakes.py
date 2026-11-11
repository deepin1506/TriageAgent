"""Test doubles. ScriptedLLM stands in for a chat model without any network calls."""

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, ValidationError

USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


def fill(schema: type[BaseModel], script: dict[str, Any]) -> BaseModel:
    """Build `schema` from the script.

    A script entry keyed by the schema's class name wins. Otherwise the script is treated
    as a pool of values keyed by FIELD name, so tests depend on your field names (the
    contract in triage/agent/graph.py), not on your class names.
    """
    if isinstance(script.get(schema.__name__), dict):
        data = script[schema.__name__]
    else:
        data = {name: script[name] for name in schema.model_fields if name in script}
    try:
        return schema.model_validate(data)
    except ValidationError as err:
        raise AssertionError(
            f"ScriptedLLM can't fill {schema.__name__} from the test script: {err}\n"
            "Use the contract field names from triage/agent/graph.py, or add the field "
            "to the script in the test."
        ) from err


class ScriptedLLM:
    """Duck-types the bits of BaseChatModel the agent uses and records every call."""

    def __init__(self, script: dict[str, Any]) -> None:
        self.script = script
        self.calls: list[tuple[str, list[BaseMessage]]] = []  # (schema name or "text", messages)

    def with_structured_output(self, schema: type[BaseModel], include_raw: bool = False, **_: Any):
        async def run(messages: list[BaseMessage]) -> Any:
            self.calls.append((schema.__name__, list(messages)))
            value = fill(schema, self.script)
            if not include_raw:
                return value
            raw = AIMessage(content=value.model_dump_json(), usage_metadata=USAGE)
            return {"raw": raw, "parsed": value, "parsing_error": None}

        return RunnableLambda(run)

    async def ainvoke(self, messages: list[BaseMessage], **_: Any) -> AIMessage:
        """Plain-text calls (e.g. if your summarize node asks for free text)."""
        self.calls.append(("text", list(messages)))
        return AIMessage(content=self.script.get("summary", "Summary."), usage_metadata=USAGE)

    def prompts_for(self, schema_name: str) -> list[str]:
        """All message text sent for one schema, for asserting on prompt content."""
        return [str(m.content) for name, msgs in self.calls if name == schema_name for m in msgs]
