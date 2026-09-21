"""`retrieve_knowledge`: Phase 4's central change, per `docs/ARCHITECTURE.md`
§5.2. `RetrievalService` (Phase 3) is unchanged underneath; what changes is
who decides to call it. Retrieval used to run unconditionally before every
chat turn, injecting chunks into prompts that never needed them -- "hi"
paid for a vector search it had no use for, and Phase 3's own review found
the system citing sources for "can you write me a poem about the sea". A
tool the model chooses to call fixes that at the root, instead of papering
over it with a relevance floor after the fact.

This also means a real model, not a trusted prompt-assembly step, now
supplies `query` and `top_k` as free-form arguments -- see `_MAX_TOP_K` and
the empty-result handling below for the two places that matters most.
"""

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.embeddings.base import EmbeddingProvider
from app.prompts.context import assemble_context
from app.rag.retrieve import RetrievalService, build_citation
from app.tools.base import AgentTool, ToolContext, ToolResult

# `top_k` reaches this tool as an argument a model chose, not a value a
# trusted caller passed -- nothing stops it asking for 1000. Unlike
# `RetrievalService.retrieve`'s own `candidates` (also 20, capping how many
# rows *feed* fusion), this caps how many chunks the tool will ever hand
# back to the model: even a corpus with hundreds of `ready` chunks that all
# clear the relevance floor must not turn one tool call into a
# multi-thousand-token prompt injection of the entire knowledge base.
_MAX_TOP_K = 10

# §5.2: "retrieve_knowledge returning an explicit 'no relevant knowledge
# found' payload rather than an empty list the model can paper over" is
# named as the specific mechanism that stops the model quietly answering
# from parametric memory when the corpus has nothing. An empty `content`
# string or an empty `citations` list alone does not carry that meaning to
# the model reading it on its next turn -- this message does.
_NO_RESULTS_MESSAGE = (
    "No relevant knowledge found for this query. Do not answer from general "
    "knowledge; tell the user this information isn't available."
)


class RetrieveKnowledgeArgs(BaseModel):
    query: str = Field(
        description="A focused search query for the organization's knowledge base. "
        "Rewrite the user's question into a standalone query (e.g. resolve pronouns "
        "against the conversation) rather than passing their words through verbatim."
    )
    top_k: int = Field(
        default=5,
        description="Maximum number of passages to return.",
    )


class RetrieveKnowledgeTool(AgentTool):
    name = "retrieve_knowledge"
    description = (
        "Search the organization's own documents for information relevant to a "
        "question. Call this when answering requires company-specific knowledge "
        "(policies, products, pricing, procedures) rather than general conversation. "
        "Returns ranked passages with citations, or an explicit message when nothing "
        "relevant is found."
    )
    args_model = RetrieveKnowledgeArgs

    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider | None = None) -> None:
        # `session` is the caller's own, already tenant-bound (RLS-scoped)
        # session -- the same one `ChatService` holds for the rest of the
        # turn -- not one this tool opens for itself. That is what makes the
        # savepoint in `execute` meaningful: a DB-level failure here would
        # otherwise poison every later statement the caller runs on it.
        self.session = session
        self.embedder = embedder

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, RetrieveKnowledgeArgs)
        # Clamped, not validated-and-rejected: a model asking for too many
        # results is not a malformed call worth bouncing back as an error
        # for it to retry (unlike, say, a missing required field) -- it is
        # simply capped to what the tool will ever hand back. `max(1, ...)`
        # guards the other end (`top_k=0` or negative), which
        # `RetrievalService.retrieve` would otherwise quietly turn into zero
        # results rather than a clamp to "at least one".
        top_k = max(1, min(args.top_k, _MAX_TOP_K))
        # `ToolContext` carries only `organization_id`, never a full
        # `TenantContext` -- `user_id`/`role` describe an authenticated
        # staff user, which a tool call (possibly from an anonymous chat
        # widget visitor) has no equivalent of. `RetrievalService` only ever
        # reads `.organization_id` off this, so the rest is left `None`.
        tenant = TenantContext(
            organization_id=ctx.organization_id,
            user_id=None,
            role=None,
            request_id=ctx.request_id,
        )

        # `self.session` is `ChatService.session` -- shared across every
        # tool call this whole turn makes (see `app/chat/service.py`'s
        # `_LockedSessionTool`, which serializes concurrent access to it
        # rather than giving each call its own session, precisely so a
        # write earlier in this same turn stays visible to a later call).
        # Postgres aborts the whole surrounding transaction on any
        # statement error, not just the failing one, so without this
        # savepoint a single bad row (a wrong-dimension embedding, a
        # momentarily-unavailable `vector` extension) would poison every
        # later statement the *caller's* transaction runs -- the
        # conversation's own history/usage writes included -- over a
        # single tool call the model made. `begin_nested()` opens a SAVEPOINT
        # scoped to this block; SQLAlchemy rolls back to it automatically
        # when an exception propagates out of the `async with`, leaving the
        # outer transaction fully usable. The exception itself is left to
        # propagate -- deliberately not caught here -- because
        # `ToolRegistry.execute` already turns any exception raised out of
        # `execute` into `ToolResult(is_error=True)` (see its docstring);
        # catching it again here would just be the same behaviour duplicated
        # one layer down.
        async with self.session.begin_nested():
            chunks = await RetrievalService(self.session, tenant, self.embedder).retrieve(
                args.query, top_k=top_k
            )

        if not chunks:
            return ToolResult(content=_NO_RESULTS_MESSAGE)

        # `assemble_context` is `app/prompts/context.py`'s system-prompt
        # renderer, reused verbatim here rather than re-implemented: a tool
        # result is fed back into the conversation exactly like a system
        # prompt's retrieved block, so it carries the identical injection
        # hazard (a document's own text -- and title -- is attacker-
        # controlled, since a tenant's own user supplied both at upload
        # time). Re-rolling a second, tool-flavoured passage formatter here
        # would mean a future fix to that mitigation (see its docstring)
        # covering only one of the two paths retrieved text now reaches the
        # model through.
        return ToolResult(
            content=assemble_context(chunks),
            citations=[build_citation(chunk) for chunk in chunks],
        )
