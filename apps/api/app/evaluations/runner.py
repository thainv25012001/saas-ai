"""Running an evaluation: each case through the real production turn
(docs/PHASE-6.md §2, §5).

Orchestration only -- which session each step runs in, and in what order.
The rules (what a status transition may do, what a result row looks like,
the summary) live on `EvaluationService`; scoring lives in `scorers.py` and
`judge.py`.

Per case, three transactions:

1. a short `tenant_session` re-reading the run's status, so a cancel is
   honoured between cases;
2. a `rolled_back_tenant_session` driving `ChatService.send` -- unmodified,
   so the agent sees exactly what production would show it, `create_lead`
   genuinely succeeding included -- and then reading, *before* the rollback,
   the full tool-call rows that turn wrote. Everything the turn wrote
   (conversation, messages, tool calls, citations, its `usage_events` row,
   any lead) is discarded when that block exits;
3. a fresh `tenant_session` writing the `eval_results` row and the spend.

Only a failure outside any one case's own handling (the run row gone, the
database unreachable) fails the run -- marked through an independent
session, then re-raised. A turn that errors is that case's result, not the
run's failure.

Nothing here logs case content: ids, counts, durations and scores only.
"""

import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.service import (
    ChatCitations,
    ChatError,
    ChatMessageEnd,
    ChatMessageStart,
    ChatService,
    ChatTextDelta,
    ChatToolCall,
    ChatToolCallEnd,
    ChatToolCallResult,
    ChatToolCallStart,
)
from app.conversations.schemas import RecordUsageInput
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.tenancy import TenantContext, rolled_back_tenant_session, tenant_session
from app.db.models import (
    ConversationChannel,
    EvalCase,
    EvalRunStatus,
    MessageToolCall,
    UsageEvent,
    UsageKind,
)
from app.evaluations.judge import Judge
from app.evaluations.scorers import (
    Expectations,
    Observation,
    ObservedToolCall,
    ScoreResult,
    case_passed,
    deterministic_scores,
    score_to_json,
)
from app.evaluations.service import EvaluationService, RecordResultInput
from app.llm.base import LLMProvider
from app.llm.pricing import estimate_cost
from app.llm.registry import get_provider
from app.llm.types import Usage
from app.rag.ingest import bounded_error_message
from app.rag.retrieve import excerpt

logger = get_logger(__name__)

# `eval_results.error` is rendered in the dashboard; a provider's message is
# already normalised (`app/llm/errors.py`), this only bounds its length.
_CASE_ERROR_MAX_CHARS = 1000


@dataclass(frozen=True, slots=True)
class _PinnedRun:
    """What `claim_run` pinned, copied off the ORM row so nothing below
    touches an instance whose session has closed."""

    agent_id: uuid.UUID
    dataset_id: uuid.UUID
    prompt_version_id: uuid.UUID | None
    provider: str
    model: str
    judge_provider: str | None
    judge_model: str | None


@dataclass(frozen=True, slots=True)
class _Case:
    id: uuid.UUID
    question: str
    expectations: Expectations


@dataclass(frozen=True, slots=True)
class _TurnUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class _Turn:
    observation: Observation
    #: `None` when the turn errored before reporting any usage -- there is
    #: no figure to bill, so no `usage_events` row is written for it.
    usage: _TurnUsage | None
    latency_ms: int
    prompt_version_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class _JudgeVerdict:
    result: ScoreResult
    #: `None` when no provider call was made at all (the judge's provider
    #: could not even be resolved) -- nothing was spent.
    usage: Usage | None
    cost_usd: Decimal | None


async def run_evaluation(
    tenant: TenantContext,
    run_id: uuid.UUID,
    *,
    provider_override: LLMProvider | None = None,
    judge_provider_override: LLMProvider | None = None,
) -> None:
    """Run (or resume) one evaluation run to completion.

    `provider_override`/`judge_provider_override` inject whole `LLMProvider`
    objects -- tests only, exactly like `ChatService`'s `provider_override`.
    Without them the run's pinned `provider`/`judge_provider` names are
    resolved through the registry.

    Safe to call again for the same run: a terminal run returns at once, and
    a `running` one (a worker that died part-way, retried by arq) resumes,
    skipping every case that already holds a result.
    """
    started_at = time.monotonic()
    try:
        async with tenant_session(tenant) as session:
            run = await EvaluationService(session, tenant).claim_run(run_id)
            if run is None:
                return
            pinned = _PinnedRun(
                agent_id=run.agent_id,
                dataset_id=run.dataset_id,
                prompt_version_id=run.prompt_version_id,
                provider=run.provider,
                model=run.model,
                judge_provider=run.judge_provider,
                judge_model=run.judge_model,
            )
        logger.info(
            "evaluation_run_started",
            run_id=str(run_id),
            organization_id=str(tenant.organization_id),
        )

        async with tenant_session(tenant) as session:
            service = EvaluationService(session, tenant)
            cases = [_snapshot(case) for case in await service.list_cases(pinned.dataset_id)]
            recorded = await service.recorded_case_ids(run_id)

        judge, judge_unavailable = _build_judge(pinned, judge_provider_override)

        for case in cases:
            if case.id in recorded:
                continue
            async with tenant_session(tenant) as session:
                status = await EvaluationService(session, tenant).run_status(run_id)
            if status is not EvalRunStatus.RUNNING:
                # Cancelled (or otherwise finished) since the last case --
                # checked between cases, never mid-turn (§5).
                logger.info("evaluation_run_stopped", run_id=str(run_id), status=status.value)
                break
            await _run_case(
                tenant,
                run_id,
                pinned,
                case,
                provider_override=provider_override,
                judge=judge,
                judge_unavailable=judge_unavailable,
            )

        async with tenant_session(tenant) as session:
            completed = await EvaluationService(session, tenant).complete_run(run_id)
            if completed is not None:
                summary = completed.summary
                logger.info(
                    "evaluation_run_completed",
                    run_id=str(run_id),
                    organization_id=str(tenant.organization_id),
                    case_count=completed.case_count,
                    completed_count=completed.completed_count,
                    passed=summary.get("passed"),
                    failed=summary.get("failed"),
                    errored=summary.get("errored"),
                    pass_rate=summary.get("pass_rate"),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                )
    except Exception as exc:
        error = bounded_error_message(exc)
        await _mark_failed(tenant, run_id, error)
        logger.error(
            "evaluation_run_failed",
            run_id=str(run_id),
            organization_id=str(tenant.organization_id),
            duration_ms=int((time.monotonic() - started_at) * 1000),
            # Bounded, and deliberately no `exc_info` -- see
            # `ingest_document_task` for why a traceback would leak content.
            error=error,
        )
        raise


async def _mark_failed(tenant: TenantContext, run_id: uuid.UUID, error: str) -> None:
    """An independent session: whatever broke may have taken the session it
    broke on with it. Its own failure is logged, never raised -- the
    original exception is the one the caller must see."""
    try:
        async with tenant_session(tenant) as session:
            await EvaluationService(session, tenant).mark_failed(run_id, error)
    except Exception as exc:
        logger.error(
            "evaluation_run_mark_failed_failed",
            run_id=str(run_id),
            error=bounded_error_message(exc),
        )


def _snapshot(case: EvalCase) -> _Case:
    return _Case(
        id=case.id,
        question=case.question,
        expectations=Expectations(
            reference_answer=case.reference_answer,
            required_phrases=list(case.required_phrases),
            expected_tool_names=list(case.expected_tool_names),
            expected_document_ids=list(case.expected_document_ids),
            expected_product_ids=list(case.expected_product_ids),
        ),
    )


def _build_judge(
    pinned: _PinnedRun, override: LLMProvider | None
) -> tuple[Judge | None, str | None]:
    """`(judge, None)`, `(None, None)` for a run with no judge, or
    `(None, error_name)` when the judge's provider cannot be resolved (its
    key was removed after the run started) -- every judged case then scores
    the judge as `error`, the same as a judge call that failed."""
    if pinned.judge_provider is None or pinned.judge_model is None:
        return None, None
    try:
        provider = override or get_provider(pinned.judge_provider)
    except AppError as exc:
        return None, type(exc).__name__
    return Judge(provider, pinned.judge_model), None


async def _run_case(
    tenant: TenantContext,
    run_id: uuid.UUID,
    pinned: _PinnedRun,
    case: _Case,
    *,
    provider_override: LLMProvider | None,
    judge: Judge | None,
    judge_unavailable: str | None,
) -> None:
    turn = await _run_turn(tenant, pinned, case, provider_override)
    observation = turn.observation
    scores = deterministic_scores(case.expectations, observation)

    verdict: _JudgeVerdict | None = None
    reference_answer = case.expectations.reference_answer
    # Not judged when the turn errored: the case fails regardless
    # (`case_passed`), and grading an answer that never finished would be
    # spend for nothing.
    if reference_answer and observation.error is None:
        if judge is not None:
            outcome = await judge.score(case.question, reference_answer, observation)
            assert pinned.judge_model is not None
            verdict = _JudgeVerdict(
                result=outcome.result,
                usage=outcome.usage,
                cost_usd=estimate_cost(pinned.judge_model, outcome.usage),
            )
        elif judge_unavailable is not None:
            verdict = _JudgeVerdict(
                result=ScoreResult(
                    score=None,
                    passed=False,
                    status="error",
                    detail={"error": judge_unavailable},
                ),
                usage=None,
                cost_usd=None,
            )
    if verdict is not None:
        scores["judge"] = verdict.result
    passed = case_passed(scores, observation)

    usage_rows: list[RecordUsageInput] = []
    if turn.usage is not None:
        usage_rows.append(
            RecordUsageInput(
                agent_id=pinned.agent_id,
                # The conversation never persisted (§2).
                conversation_id=None,
                kind=UsageKind.LLM,
                provider=pinned.provider,
                model=pinned.model,
                input_tokens=turn.usage.input_tokens,
                output_tokens=turn.usage.output_tokens,
                cost_usd=turn.usage.cost_usd,
            )
        )
    if verdict is not None and verdict.usage is not None:
        assert pinned.judge_provider is not None and pinned.judge_model is not None
        usage_rows.append(
            RecordUsageInput(
                agent_id=pinned.agent_id,
                conversation_id=None,
                kind=UsageKind.LLM,
                provider=pinned.judge_provider,
                model=pinned.judge_model,
                input_tokens=verdict.usage.input_tokens,
                output_tokens=verdict.usage.output_tokens,
                cost_usd=verdict.cost_usd,
            )
        )

    # Turn cost plus judge cost; unknown if either part is.
    cost: Decimal | None = turn.usage.cost_usd if turn.usage is not None else None
    if cost is not None and verdict is not None:
        cost = cost + verdict.cost_usd if verdict.cost_usd is not None else None

    async with tenant_session(tenant) as session:
        inserted = await EvaluationService(session, tenant).record_result(
            RecordResultInput(
                run_id=run_id,
                case_id=case.id,
                question=case.question,
                answer=observation.answer,
                error=observation.error,
                scores={key: score_to_json(result) for key, result in scores.items()},
                passed=passed,
                tool_calls=[
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "is_error": call.is_error,
                        "excerpt": excerpt(call.content),
                    }
                    for call in observation.tool_calls
                ],
                cited_document_ids=observation.cited_document_ids,
                cited_product_ids=observation.cited_product_ids,
                prompt_version_id=turn.prompt_version_id,
                latency_ms=turn.latency_ms,
                input_tokens=turn.usage.input_tokens if turn.usage is not None else None,
                output_tokens=turn.usage.output_tokens if turn.usage is not None else None,
                cost_usd=cost,
                usage=usage_rows,
            )
        )
    logger.info(
        "evaluation_case_scored",
        run_id=str(run_id),
        case_id=str(case.id),
        passed=passed,
        latency_ms=turn.latency_ms,
        errored=observation.error is not None,
        recorded=inserted,
    )


async def _run_turn(
    tenant: TenantContext,
    pinned: _PinnedRun,
    case: _Case,
    provider_override: LLMProvider | None,
) -> _Turn:
    """Drive one production turn in a transaction that is always rolled
    back, and extract what scoring needs from it before the rollback."""
    answer: list[str] = []
    error: str | None = None
    message_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    end: ChatMessageEnd | None = None
    started: list[ChatToolCall] = []
    ended: list[ChatToolCallResult] = []
    document_ids: dict[uuid.UUID, None] = {}
    product_ids: dict[uuid.UUID, None] = {}
    started_at = time.monotonic()

    async with rolled_back_tenant_session(tenant) as session:
        chat = ChatService(session, tenant, provider_override=provider_override)
        try:
            async for event in chat.send(
                pinned.agent_id,
                case.question,
                channel=ConversationChannel.API,
                override_provider=pinned.provider,
                override_model=pinned.model,
                prompt_version_id=pinned.prompt_version_id,
            ):
                if isinstance(event, ChatMessageStart):
                    message_id = event.message_id
                    conversation_id = event.conversation_id
                elif isinstance(event, ChatTextDelta):
                    answer.append(event.text)
                elif isinstance(event, ChatToolCallStart):
                    started.extend(event.calls)
                elif isinstance(event, ChatToolCallEnd):
                    ended.extend(event.results)
                elif isinstance(event, ChatCitations):
                    for citation in event.citations:
                        if citation.document_id is not None:
                            document_ids.setdefault(citation.document_id, None)
                        if citation.product_id is not None:
                            product_ids.setdefault(citation.product_id, None)
                elif isinstance(event, ChatMessageEnd):
                    end = event
                elif isinstance(event, ChatError):
                    error = f"{event.code}: {event.message}"
        except AppError as exc:
            # Raised before streaming -- the agent was deleted, the pinned
            # version no longer belongs to its prompt, a provider key was
            # removed. This case's result, not the run's failure.
            error = f"{exc.code}: {exc.message}"
        except Exception as exc:
            # Anything else escaping a turn is still this case's outcome; a
            # database that is really gone fails the result write next, and
            # with it the run.
            error = bounded_error_message(exc)
            logger.warning(
                "evaluation_case_turn_crashed",
                case_id=str(case.id),
                error_type=type(exc).__name__,
            )

        # Read before the block exits -- after it, the rows are gone.
        tool_calls = (
            await _read_tool_calls(session, tenant, message_id, started)
            if message_id is not None
            else None
        )
        if tool_calls is None:
            tool_calls = _tool_calls_from_events(started, ended)
        usage = _usage_from_end(end)
        if usage is None and conversation_id is not None:
            # A step-limit turn ends in `ChatError` rather than
            # `ChatMessageEnd`, but its usage was real and `send` did record
            # it -- in this transaction.
            usage = await _read_turn_usage(session, tenant, conversation_id)

    latency_ms = end.latency_ms if end is not None else int((time.monotonic() - started_at) * 1000)
    return _Turn(
        observation=Observation(
            answer="".join(answer),
            error=error[:_CASE_ERROR_MAX_CHARS] if error is not None else None,
            tool_calls=tool_calls,
            cited_document_ids=list(document_ids),
            cited_product_ids=list(product_ids),
        ),
        usage=usage,
        latency_ms=latency_ms,
        prompt_version_id=end.prompt_version_id if end is not None else pinned.prompt_version_id,
    )


def _usage_from_end(end: ChatMessageEnd | None) -> _TurnUsage | None:
    if end is None:
        return None
    return _TurnUsage(
        input_tokens=end.usage.input_tokens,
        output_tokens=end.usage.output_tokens,
        cost_usd=end.cost_usd,
    )


async def _read_tool_calls(
    session: AsyncSession,
    tenant: TenantContext,
    message_id: uuid.UUID,
    started: list[ChatToolCall],
) -> list[ObservedToolCall] | None:
    """The turn's `message_tool_calls` rows -- the tool's FULL result, which
    the judge's evidence needs and the `ChatToolCallEnd` excerpt is not.
    `None` if the read fails (a turn whose persistence failed can leave the
    transaction unusable), so the caller falls back to the events.

    Ordered by the order the calls were requested in: the rows share one
    flush, and `uuid7()` is only millisecond-ordered, so neither
    `created_at` nor `id` orders them."""
    try:
        result = await session.execute(
            select(MessageToolCall).where(
                MessageToolCall.message_id == message_id,
                MessageToolCall.organization_id == tenant.organization_id,
            )
        )
        rows = list(result.scalars().all())
    except SQLAlchemyError:
        return None
    position = {call.id: index for index, call in enumerate(started)}
    rows.sort(key=lambda row: position.get(row.tool_call_id, len(position)))
    observed: list[ObservedToolCall] = []
    for row in rows:
        content = row.result.get("content") if row.result else None
        if not isinstance(content, str):
            content = row.error_message or ""
        observed.append(
            ObservedToolCall(
                name=row.tool_name,
                arguments=dict(row.arguments or {}),
                is_error=row.is_error,
                content=content,
            )
        )
    return observed


def _tool_calls_from_events(
    started: list[ChatToolCall], ended: list[ChatToolCallResult]
) -> list[ObservedToolCall]:
    """The fallback: the SSE-facing events carry the arguments and an
    excerpt of each result -- less than the rows, but not nothing."""
    arguments = {call.id: call.arguments for call in started}
    return [
        ObservedToolCall(
            name=result.tool_name,
            arguments=dict(arguments.get(result.tool_call_id, {})),
            is_error=result.is_error,
            content=result.result,
        )
        for result in ended
    ]


async def _read_turn_usage(
    session: AsyncSession, tenant: TenantContext, conversation_id: uuid.UUID
) -> _TurnUsage | None:
    try:
        result = await session.execute(
            select(UsageEvent).where(
                UsageEvent.conversation_id == conversation_id,
                UsageEvent.organization_id == tenant.organization_id,
            )
        )
        rows = list(result.scalars().all())
    except SQLAlchemyError:
        return None
    if not rows:
        return None
    costs = [row.cost_usd for row in rows]
    return _TurnUsage(
        input_tokens=sum(row.input_tokens for row in rows),
        output_tokens=sum(row.output_tokens for row in rows),
        cost_usd=(
            None
            if any(c is None for c in costs)
            else sum((c for c in costs if c is not None), Decimal(0))
        ),
    )
