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
from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field
from structlog.testing import capture_logs

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


class _AliasedLeakArgs(BaseModel):
    # The wire-facing schema property is "org_id" -- only the attribute a
    # tool body actually reads (`args.organization_id`) is the forbidden name.
    organization_id: uuid.UUID = Field(alias="org_id")


class _AliasedLeakTool(AgentTool):
    name = "aliased-leak"
    description = "Hides organization_id from the schema behind an alias."
    args_model = _AliasedLeakArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="must never run")


class _NestedLeak(BaseModel):
    organization_id: str


class _NestedLeakArgs(BaseModel):
    filters: _NestedLeak


class _NestedLeakTool(AgentTool):
    name = "nested-leak"
    description = "Buries organization_id one level down, in a nested model."
    args_model = _NestedLeakArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="must never run")


class _ValidationAliasLeakArgs(BaseModel):
    # The mirror image of `_AliasedLeakArgs`: the attribute name is innocuous
    # ("sneaky_org"), but the model is shown, and can populate, a wire
    # argument literally named "organization_id".
    sneaky_org: uuid.UUID = Field(validation_alias="organization_id")


class _ValidationAliasLeakTool(AgentTool):
    name = "validation-alias-leak"
    description = "Clean attribute name, but validation_alias='organization_id'."
    args_model = _ValidationAliasLeakArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="must never run")


class _AliasChoicesLeakArgs(BaseModel):
    # The rendered JSON schema shows only the first choice
    # ("sneaky_org_wire") as the property name -- "organization_id" is still
    # live input, just invisible to anyone reading the schema.
    sneaky_org: uuid.UUID = Field(
        validation_alias=AliasChoices("sneaky_org_wire", "organization_id")
    )


class _AliasChoicesLeakTool(AgentTool):
    name = "alias-choices-leak"
    description = "Offers organization_id as one of several accepted wire names."
    args_model = _AliasChoicesLeakArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="must never run")


class _AliasPathLeakArgs(BaseModel):
    sneaky_org: uuid.UUID = Field(validation_alias=AliasPath("filters", "organization_id"))


class _AliasPathLeakTool(AgentTool):
    name = "alias-path-leak"
    description = "Reaches organization_id through a nested-input alias path."
    args_model = _AliasPathLeakArgs

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="must never run")


class _ExtraAllowArgs(BaseModel):
    # Declares no fields at all -- `organization_id` would ride in as an
    # undeclared key, invisible to any check of declared fields or schema.
    model_config = ConfigDict(extra="allow")


class _ExtraAllowTool(AgentTool):
    name = "extra-allow"
    description = "Accepts any undeclared field, organization_id included."
    args_model = _ExtraAllowArgs

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


async def test_invalid_args_log_the_errors_without_the_submitted_input() -> None:
    """docs/PHASE-7.md §7: pydantic's error dicts carry `input` -- here the
    whole arguments dict, customer text included -- and `url`. Neither may
    reach `tool_call_invalid_args` (Ruling R6)."""
    registry = _registry(_EchoTool())
    sentinel = "SENTINEL-customer-secret-9b1c"
    call = ToolUseBlock(id="t1", name="echo", input={"country": sentinel})  # missing "city"

    with capture_logs() as entries:
        result = await registry.execute(call, _ctx())

    assert result.is_error is True
    [entry] = [e for e in entries if e["event"] == "tool_call_invalid_args"]
    assert entry["errors"][0]["loc"] == ("city",)
    assert all("input" not in error and "url" not in error for error in entry["errors"])
    assert sentinel not in str(entries)


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
    assert "did not finish in time" in result.content.lower()
    # The widened OUTER bound must not be quoted at the model as if it were
    # the tool's own configured budget -- whole-branch review, carried item.
    assert "0.05" not in result.content


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


def test_registering_a_tool_with_an_aliased_organization_id_field_is_rejected() -> None:
    # A schema-only check (reading `properties`) would see "org_id" and miss
    # this entirely, while `args.organization_id` is fully populated.
    with pytest.raises(ValueError, match="organization_id"):
        _registry(_AliasedLeakTool())


def test_registering_a_tool_with_organization_id_buried_in_a_nested_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="organization_id"):
        _registry(_NestedLeakTool())


def test_registering_a_tool_that_allows_extra_fields_is_rejected() -> None:
    with pytest.raises(ValueError, match="extra"):
        _registry(_ExtraAllowTool())


def test_registering_a_tool_with_organization_id_as_a_validation_alias_is_rejected() -> None:
    with pytest.raises(ValueError, match="organization_id"):
        _registry(_ValidationAliasLeakTool())


def test_registering_a_tool_offering_organization_id_as_an_alias_choice_is_rejected() -> None:
    with pytest.raises(ValueError, match="organization_id"):
        _registry(_AliasChoicesLeakTool())


def test_registering_a_tool_reaching_organization_id_through_an_alias_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="organization_id"):
        _registry(_AliasPathLeakTool())
