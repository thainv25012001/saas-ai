"""Turns registered `AgentTool`s into provider tool specs, and runs a
model's `tool_use` block against them.

Shaped like `app.llm.registry`: a plain dict keyed by name, mutated by
`register`, resolved with `.get` rather than `[]` wherever a miss must not
raise. The one place that *does* still raise -- `register`, on a tool that
asks the model for `organization_id` -- is deliberate: that is a tool
author's bug caught at wiring time, not a model's mistake caught at
run time, and those two failure domains get different treatment throughout
this module. See `execute`'s docstring for why every *runtime* miss instead
becomes a `ToolResult(is_error=True)`.
"""

import asyncio
from collections.abc import Iterator
from functools import cache
from typing import Any, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import format_validation_errors
from app.core.logging import get_logger
from app.llm.types import ToolSpec, ToolUseBlock
from app.tools.base import AgentTool, ToolContext, ToolResult

logger = get_logger(__name__)

#: The one field a tool's own arguments may never carry, at any depth. Kept as
#: a name, not a schema key, because that is exactly the distinction that
#: matters -- see `_reject_tenant_leaking_model` below.
_FORBIDDEN_ARG_FIELD = "organization_id"


def _nested_models(annotation: object) -> Iterator[type[BaseModel]]:
    """Every `BaseModel` reachable from a field's type annotation.

    Walks generic containers (`list[X]`, `X | None`, `dict[str, X]`, ...) via
    `get_origin`/`get_args` rather than reading the JSON-schema's `$defs`:
    the schema keys a nested model's own fields by their *alias* too, so
    resolving through it would reproduce the exact blind spot this function
    exists to close one level down. Walking the Python types directly finds
    the attribute name at every depth, regardless of how any level aliases it.
    """
    origin = get_origin(annotation)
    if origin is not None:
        for arg in get_args(annotation):
            yield from _nested_models(arg)
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation


def _alias_names(alias: "str | AliasPath | AliasChoices | None") -> Iterator[str]:
    """Every plain wire name an alias value could match against.

    `field_info.alias` and `field_info.serialization_alias` are always a
    plain string when set. `field_info.validation_alias` can be that too, but
    can also be an `AliasChoices` -- several wire names that all validate,
    of which the rendered JSON schema shows only the *first* as the
    property key, leaving the rest just as live as input but invisible to
    anyone reading the schema -- or an `AliasPath`, a path into nested input
    (e.g. `AliasPath("filters", "organization_id")`) whose segments can be
    strings (keys) or ints (list indices); only the string segments are
    names to check.
    """
    if isinstance(alias, str):
        yield alias
    elif isinstance(alias, AliasChoices):
        for choice in alias.choices:
            yield from _alias_names(choice)
    elif isinstance(alias, AliasPath):
        for segment in alias.path:
            if isinstance(segment, str):
                yield segment


def _reject_tenant_leaking_model(
    tool_name: str, model: type[BaseModel], seen: set[type[BaseModel]]
) -> None:
    """Raise if `model`, or any model nested inside it, could carry
    `organization_id` from model-supplied input.

    "Carry organization_id" is checked in **both** of the two namespaces a
    field lives in, because each one governs a different half of the
    guarantee, and checking only one moves the hole rather than closing it:

    - the **attribute** name (`model_fields`' keys) -- what a tool body
      reads (`args.organization_id`) -- which an alias renames on the wire
      without changing;
    - every **wire** name the field accepts (`field_info.alias`,
      `validation_alias`, `serialization_alias`, and everything inside an
      `AliasChoices`/`AliasPath`) -- what the model is shown and can
      populate -- which a *validation alias pointed at a differently-named
      attribute* leaves the attribute name clean while the model still sees
      and can fill an argument literally called `organization_id`.

    Four routes were demonstrated against earlier versions of this check,
    each closed on its own terms rather than patched around:

    - `Field(alias="org_id")` on an `organization_id` field renames the wire
      property the schema advertises, but not the attribute a tool body
      reads. Closed by checking `model_fields` (attribute names).
    - `Field(validation_alias="organization_id")` on a field named e.g.
      `sneaky_org` is the mirror image: the attribute is clean, but the
      model is shown (and can populate) a wire argument literally named
      `organization_id` -- and if the alias is instead an `AliasChoices`
      offering `organization_id` as one of several accepted names, the
      rendered schema shows only the *first* choice as the property key, so
      reading the schema would not even reveal this one. Closed by checking
      every alias attribute via `_alias_names`, not the schema.
    - A nested model (e.g. `filters: SomeFilters` where `SomeFilters`
      declares `organization_id`) doesn't appear as a top-level property at
      all. `_nested_models` walks every field's annotation to find it.
    - `ConfigDict(extra="allow")` accepts `organization_id` as an *undeclared*
      key with no field to inspect at all -- `model_extra` after validation,
      invisible to any check of `model_fields`, aliases, or the schema.
      There is no way to allow-list "extra fields except this one name"
      against a model that accepts arbitrary keys, so `extra="allow"` is
      refused outright on every args model, independent of what it declares.

    `seen` guards a self-referential or mutually-referential model from
    recursing forever *within one walk*. Repeat walks of the same class
    are avoided a level up, by `_assert_args_model_is_safe` -- see there
    for why that matters once a registry is rebuilt per turn.
    """
    if model in seen:
        return
    seen.add(model)

    if model.model_config.get("extra") == "allow":
        raise ValueError(
            f"tool '{tool_name}' args_model '{model.__name__}' sets "
            "extra='allow'; an argument model populated from model output "
            "must declare every field it accepts, or a tenant id could ride "
            "in as an undeclared key"
        )

    for field_name, field_info in model.model_fields.items():
        exposed_names = {
            field_name,
            *_alias_names(field_info.alias),
            *_alias_names(field_info.validation_alias),
            *_alias_names(field_info.serialization_alias),
        }
        if _FORBIDDEN_ARG_FIELD in exposed_names:
            raise ValueError(
                f"tool '{tool_name}' args_model '{model.__name__}' exposes "
                f"'{_FORBIDDEN_ARG_FIELD}' as a field name or alias; tenancy "
                "comes from ToolContext, never from a model-supplied argument"
            )
        for nested in _nested_models(field_info.annotation):
            _reject_tenant_leaking_model(tool_name, nested, seen)


@cache
def _assert_args_model_is_safe(tool_name: str, args_model: type[BaseModel]) -> None:
    """`_reject_tenant_leaking_model`, memoised per `(tool_name, args_model)`.

    The check walks every field, alias and nested model of an argument
    schema, and its answer is a pure function of the class -- it cannot
    differ between two calls for the same one. `ChatService` builds a fresh
    `ToolRegistry` per turn (deliberately: the per-turn lock and session
    belong to that turn), so without this the walk ran again on every chat
    turn for output that was identical every time.

    A failure is not memoised, because `lru_cache` does not cache raised
    exceptions -- a rejected model is re-walked and re-rejected on each
    attempt, which is what we want for something that should never reach
    production anyway.
    """
    _reject_tenant_leaking_model(tool_name, args_model, set())


@cache
def _input_schema(args_model: type[BaseModel]) -> dict[str, Any]:
    """`model_json_schema()`, memoised per class, for the same reason.

    Pydantic's schema generation is a recursive reflection pass, and the
    provider-facing schema for a given `args_model` is fixed for the life of
    the process. Computing it once per class rather than once per turn is
    the whole of the saving; the result is never mutated by callers.
    """
    return args_model.model_json_schema()


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        """Add a tool, keyed by its own `name`.

        Refuses an `args_model` that could carry `organization_id` from
        model-supplied input, at any nesting depth or under any alias, and
        refuses one that accepts undeclared fields at all -- see
        `_reject_tenant_leaking_model` for the three routes this closes and
        why each is closed where it is. Checking this here (once, at
        registration) rather than trusting every future tool author to
        remember the rule is what makes the guarantee "a model has no
        argument through which to ask for another tenant's data" hold for
        tools nobody has written yet.
        """
        _assert_args_model_is_safe(tool.name, tool.args_model)
        self._tools[tool.name] = tool

    def specs_for(self, names: list[str]) -> list[ToolSpec]:
        """Provider-facing specs for exactly the requested tools, in order.

        Each `input_schema` comes straight from `args_model.model_json_schema()`
        -- never hand-written -- so what the model is shown can never drift
        from the validation `execute` runs against that same model.
        """
        return [
            ToolSpec(
                name=tool.name,
                description=tool.description,
                input_schema=_input_schema(tool.args_model),
            )
            for tool in (self._tools[name] for name in names)
        ]

    async def execute(self, call: ToolUseBlock, ctx: ToolContext) -> ToolResult:
        """Run one `tool_use` block, never letting the model's mistake -- or
        the tool's -- become an exception that ends the turn (§7.3).

        Three failure modes are handled here, each turned into
        `ToolResult(is_error=True)` rather than raised: a hallucinated tool
        name, arguments that fail `args_model` validation, and a call that
        outruns `timeout_seconds`. A fourth -- the tool's own `execute`
        raising, e.g. a downstream dependency breaking -- is caught too:
        `docs/ARCHITECTURE.md` §7.3 says a failed tool result, not a
        traceback, is what lets the model say "unable to check live
        inventory" instead of the whole conversation dying on it.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            logger.warning("tool_call_unknown_name", tool_name=call.name, **ctx.log_fields())
            return ToolResult(content=f"unknown tool '{call.name}'", is_error=True)

        try:
            args = tool.args_model.model_validate(call.input)
        except PydanticValidationError as exc:
            logger.info(
                "tool_call_invalid_args",
                tool_name=call.name,
                errors=exc.errors(),
                **ctx.log_fields(),
            )
            return ToolResult(
                content=f"invalid arguments for '{call.name}': "
                f"{format_validation_errors(exc.errors())}",
                is_error=True,
            )

        try:
            async with asyncio.timeout(tool.timeout_seconds):
                return await tool.execute(args, ctx)
        except TimeoutError:
            logger.warning(
                "tool_call_timed_out",
                tool_name=call.name,
                timeout_seconds=tool.timeout_seconds,
                **ctx.log_fields(),
            )
            # The number is deliberately NOT in the message. `tool.
            # timeout_seconds` here is the OUTER bound, which
            # `app.tools.runtime.LockedSessionTool` widens by its lock-wait
            # budget to allow for queueing -- so this path used to tell the
            # model (and whoever read the transcript) that a tool "timed out
            # after 40.0s" against a configured budget of 10s, a figure that
            # appears in no configuration file anywhere. This path only fires
            # when something is structurally wrong (a leaked lock, a hung
            # sibling), which is exactly when a misleading number costs the
            # most diagnostic time. The real figure is on the log line above,
            # where it belongs.
            return ToolResult(
                content=f"'{call.name}' did not finish in time and was stopped.", is_error=True
            )
        except Exception:
            # Deliberately broad: this is the boundary between "a tool's
            # implementation" and "the conversation loop", and nothing a
            # tool's own code does downstream (a DB error, a flaky HTTP
            # call) may propagate past it. `logger.exception` captures the
            # traceback for operators; the model only ever sees the plain
            # message §7.3 asks for.
            logger.exception("tool_call_raised", tool_name=call.name, **ctx.log_fields())
            return ToolResult(content=f"'{call.name}' failed unexpectedly", is_error=True)
