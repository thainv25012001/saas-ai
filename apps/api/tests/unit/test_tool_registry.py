"""`app/tools`: turning an `AgentTool` into a provider-facing schema
(`ToolRegistry.specs_for`) and running a model's tool call against it
(`ToolRegistry.execute`) without ever letting a recoverable mistake --
malformed arguments, a hallucinated name, a slow backend, a buggy tool --
end the conversation. Per `docs/ARCHITECTURE.md` §7.3, every one of those
comes back as `ToolResult(is_error=True)`, not an exception.
"""

import asyncio
import uuid

import pytest
from anyio import fail_after
from pydantic import BaseModel

from app.llm.types import ToolUseBlock
from app.tools.base import AgentTool, ToolContext, ToolResult
from app.tools.registry import ToolRegistry

pytestmark = pytest.mark.anyio


def _ctx(**overrides: object) -> ToolContext:
    payload: dict[str, object] = {
        "organization_id": uuid.uuid4(),
        "agent_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "request_id": "req-1",
    }
    payload.update(overrides)
    return ToolContext(**payload)  # type: ignore[arg-type]


class _EchoArgs(BaseModel):
    city: str
    country: str = "FR"


class _EchoTool(AgentTool):
    name = "echo"
    description = "Echoes back the city (and country) it is given."
    args_model = _EchoArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, _EchoArgs)
        return ToolResult(content=f"{args.city}, {args.country}")


class _NoArgs(BaseModel):
    pass


class _SlowTool(AgentTool):
    name = "slow"
    description = "Sleeps far longer than its own timeout."
    args_model = _NoArgs
    timeout_seconds = 0.05

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        await asyncio.sleep(2)
        return ToolResult(content="should never get here")


class _BoomTool(AgentTool):
    name = "boom"
    description = "Always raises, as if a downstream dependency broke."
    args_model = _NoArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("kaboom")


class _LeakyArgs(BaseModel):
    organization_id: str


class _LeakyTool(AgentTool):
    name = "leaky"
    description = "Illegally asks the model for a tenant id."
    args_model = _LeakyArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="must never run")


def _registry(*tools: AgentTool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def test_specs_for_derives_schema_from_the_args_model() -> None:
    [spec] = _registry(_EchoTool()).specs_for(["echo"])

    assert spec.name == "echo"
    assert spec.description == "Echoes back the city (and country) it is given."
    # Derived, not hand-copied: required vs. optional must match the model
    # exactly, or the provider is told one contract while validation
    # enforces another. A hand-written schema (e.g. marking both fields
    # required) would pass a weaker check but fails this one.
    assert spec.input_schema["required"] == ["city"]
    assert set(spec.input_schema["properties"]) == {"city", "country"}


def test_specs_for_returns_only_the_requested_names() -> None:
    registry = _registry(_EchoTool(), _SlowTool())

    specs = registry.specs_for(["echo"])

    assert [spec.name for spec in specs] == ["echo"]


async def test_invalid_args_are_an_error_result_naming_the_field() -> None:
    registry = _registry(_EchoTool())
    call = ToolUseBlock(id="t1", name="echo", input={"country": "FR"})  # missing "city"

    result = await registry.execute(call, _ctx())

    assert result.is_error is True
    assert "city" in result.content


async def test_a_hallucinated_tool_name_is_an_error_result_not_a_crash() -> None:
    registry = _registry(_EchoTool())
    call = ToolUseBlock(id="t1", name="not-a-real-tool", input={})

    result = await registry.execute(call, _ctx())

    assert result.is_error is True
    assert "not-a-real-tool" in result.content


async def test_a_tool_that_exceeds_its_timeout_is_an_error_and_does_not_hang() -> None:
    registry = _registry(_SlowTool())
    call = ToolUseBlock(id="t1", name="slow", input={})

    # Bounds the test itself: if the registry's own timeout regressed, this
    # fails loudly after 1s instead of the suite hanging on `_SlowTool`'s 2s
    # sleep (or longer, if a future tool sleeps longer still).
    with fail_after(1):
        result = await registry.execute(call, _ctx())

    assert result.is_error is True
    assert "timed out" in result.content.lower()


async def test_a_tool_that_raises_is_an_error_result_not_a_crash() -> None:
    registry = _registry(_BoomTool())
    call = ToolUseBlock(id="t1", name="boom", input={})

    result = await registry.execute(call, _ctx())

    assert result.is_error is True


async def test_a_successful_call_returns_the_tools_own_result() -> None:
    registry = _registry(_EchoTool())
    call = ToolUseBlock(id="t1", name="echo", input={"city": "Hanoi"})

    result = await registry.execute(call, _ctx())

    assert result.is_error is False
    assert result.content == "Hanoi, FR"


def test_registering_a_tool_that_asks_the_model_for_organization_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="organization_id"):
        _registry(_LeakyTool())
