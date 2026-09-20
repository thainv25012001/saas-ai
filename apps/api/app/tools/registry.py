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

from pydantic import ValidationError as PydanticValidationError

from app.core.errors import format_validation_errors
from app.core.logging import get_logger
from app.llm.types import ToolSpec, ToolUseBlock
from app.tools.base import AgentTool, ToolContext, ToolResult

logger = get_logger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        """Add a tool, keyed by its own `name`.

        Refuses a tool whose `args_model` declares `organization_id`: per
        `docs/ARCHITECTURE.md` §7.3, tenancy comes from `ToolContext`, which
        the server builds from the authenticated request, never from
        model-supplied arguments. Checking the *schema* here (once, at
        registration) rather than trusting every future tool author to
        remember the rule is what makes the guarantee "a model has no
        argument through which to ask for another tenant's data" hold for
        tools nobody has written yet.
        """
        schema_properties = tool.args_model.model_json_schema().get("properties", {})
        if "organization_id" in schema_properties:
            raise ValueError(
                f"tool '{tool.name}' declares 'organization_id' in its args_model; "
                "tenancy comes from ToolContext, never from a model-supplied argument"
            )
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
                input_schema=tool.args_model.model_json_schema(),
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
            logger.warning("tool_call_unknown_name", tool_name=call.name)
            return ToolResult(content=f"unknown tool '{call.name}'", is_error=True)

        try:
            args = tool.args_model.model_validate(call.input)
        except PydanticValidationError as exc:
            logger.info("tool_call_invalid_args", tool_name=call.name, errors=exc.errors())
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
                "tool_call_timed_out", tool_name=call.name, timeout_seconds=tool.timeout_seconds
            )
            return ToolResult(
                content=f"'{call.name}' timed out after {tool.timeout_seconds}s", is_error=True
            )
        except Exception:
            # Deliberately broad: this is the boundary between "a tool's
            # implementation" and "the conversation loop", and nothing a
            # tool's own code does downstream (a DB error, a flaky HTTP
            # call) may propagate past it. `logger.exception` captures the
            # traceback for operators; the model only ever sees the plain
            # message §7.3 asks for.
            logger.exception("tool_call_raised", tool_name=call.name)
            return ToolResult(content=f"'{call.name}' failed unexpectedly", is_error=True)
