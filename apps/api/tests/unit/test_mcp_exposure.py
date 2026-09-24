"""`MCP_EXPOSED_TOOL_NAMES` is the one place a tool becomes reachable over
MCP (docs/PHASE-7.md §5). These pin the two properties that make it safe:
the write tool is not in it, and nothing in it is a name the runtime could
not construct anyway."""

from app.mcp.server import MCP_EXPOSED_TOOL_NAMES
from app.tools.runtime import BUILTIN_TOOL_CLASSES


def test_create_lead_is_not_exposed_over_mcp() -> None:
    assert "create_lead" not in MCP_EXPOSED_TOOL_NAMES


def test_the_exposed_set_is_exactly_the_three_read_tools() -> None:
    assert MCP_EXPOSED_TOOL_NAMES == frozenset(
        {"search_products", "get_product", "retrieve_knowledge"}
    )


def test_every_exposed_name_is_a_real_builtin() -> None:
    builtin_names = {tool_cls.name for tool_cls in BUILTIN_TOOL_CLASSES}
    assert MCP_EXPOSED_TOOL_NAMES <= builtin_names
