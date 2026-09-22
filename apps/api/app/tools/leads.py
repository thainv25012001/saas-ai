"""`create_lead`: the only tool in this system that writes data, and the
only one an untrusted chat-widget visitor can trigger (`docs/ARCHITECTURE.md`
§7.2). Everything else in Phase 4 reads; this is what turns a conversation
into a row a human can actually follow up on.
"""

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RateLimitError
from app.core.rate_limit import enforce_rate_limit
from app.core.tenancy import TenantContext
from app.leads.schemas import CreateLeadInput
from app.leads.service import LeadService
from app.tools.base import AgentTool, ToolContext, ToolResult

# Per conversation, not per organization or per visitor: §7.2 asks for
# per-conversation limiting specifically, and `conversation_id` is the only
# stable identity `ToolContext` carries for an anonymous widget visitor --
# `visitor_id` is optional and caller-supplied, so it is not something a
# limiter can rely on existing. A handful of calls is already generous for
# one real conversation: a visitor confirming or correcting their own
# contact details a couple of times is normal; more than that in the same
# conversation is a loop, not a person typing.
_RATE_LIMIT = 3
_RATE_LIMIT_WINDOW_SECONDS = 300


class CreateLeadTool(AgentTool):
    name = "create_lead"
    description = (
        "Record a prospective customer's contact details and what they're interested in. "
        "Requires a name and at least one of email or phone -- ask for whichever is "
        "missing before calling this. Confirm the details back to the visitor first: "
        "this writes a real record that a human will follow up on."
    )
    args_model = CreateLeadInput

    def __init__(self, session: AsyncSession) -> None:
        # Same idiom as `RetrieveKnowledgeTool`: the caller's own,
        # already tenant-bound session -- not one this tool opens for
        # itself -- so the savepoint in `execute` protects the turn's
        # surrounding transaction rather than a throwaway one of its own.
        self.session = session

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, CreateLeadInput)

        # Checked first, and outside the savepoint below: a call rejected
        # here never touches the database, so there is nothing yet for a
        # savepoint to protect. §7.2/§7.3: exceeding the limit must be a
        # result the model can read and relay ("please try again shortly"),
        # never an exception that ends the whole turn -- `enforce_rate_limit`
        # raises `RateLimitError` (an `AppError`, not the DB/Redis-outage
        # case, which it already handles by failing open internally), so
        # that is the one exception this tool catches itself rather than
        # leaving to `ToolRegistry.execute`'s generic handler: the registry's
        # generic message ("'create_lead' failed unexpectedly") would tell
        # the model nothing about *why*, exactly the failure mode item 2 of
        # the brief warns against for validation and applies here just the
        # same.
        try:
            await enforce_rate_limit(
                f"create_lead:{ctx.conversation_id}",
                limit=_RATE_LIMIT,
                window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
            )
        except RateLimitError as exc:
            return ToolResult(content=str(exc), is_error=True)

        # `ToolContext` carries only `organization_id`, never a full
        # `TenantContext` -- see `RetrieveKnowledgeTool.execute`'s identical
        # comment. `LeadService` only ever reads `.organization_id` off
        # this, so the rest is left `None`.
        tenant = TenantContext(
            organization_id=ctx.organization_id,
            user_id=None,
            role=None,
            request_id=ctx.request_id,
        )

        # See `RetrieveKnowledgeTool.execute`'s docstring for why this
        # savepoint exists verbatim -- the identical hazard, on a write
        # instead of a read: a DB-level failure here (a constraint the
        # model somehow tripped, a momentary connection blip) must not
        # poison the rest of the turn's transaction, which for `create_lead`
        # still includes the conversation's own history the caller persists
        # after this tool call returns. `LeadService.create`'s own
        # `NotFoundError` (a cross-tenant `conversation_id` -- see its
        # docstring for why that check exists at all) is a plain Python
        # exception, not a failing SQL statement, but letting it unwind
        # through this same `async with` is still correct: it rolls back to
        # the savepoint before propagating, exactly like a SQL-level
        # failure would, and `ToolRegistry.execute` already turns any
        # exception raised out of `execute` into `ToolResult(is_error=True)`
        # -- nothing here needs to catch it a second time.
        async with self.session.begin_nested():
            lead = await LeadService(self.session, tenant).create(
                ctx.agent_id, ctx.conversation_id, args
            )

        return ToolResult(
            content=f"Lead recorded for {lead.name}.",
            data={"lead_id": str(lead.id), "status": lead.status.value},
        )
