"""Task 4: `run_evaluation` -- each case driven through the real
`ChatService.send` inside an always-rolled-back transaction, then scored and
recorded in an independent one (docs/PHASE-6.md §2, §5).

Every case's turn is scripted with `FakeProvider(turns=...)` (or a thin
subclass of it that counts, records or fails individual calls); the judge is
a stub whose `generate()` returns a canned verdict. Nothing here touches the
network.

Cases are created one per transaction, never several in one: `created_at`
is the transaction's start time and `uuid7()` is only millisecond-ordered,
so cases sharing a transaction have no deterministic order -- and every test
below scripts provider turns in case order.
"""

import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from app.agents.service import AgentService
from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import DocumentSourceType, EvalResult, EvalRun, EvalRunStatus
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService
from app.embeddings.hashing import HashingEmbedder
from app.evaluations import runner as runner_module
from app.evaluations.runner import run_evaluation
from app.evaluations.schemas import CaseInput, CreateDatasetInput, StartRunInput
from app.evaluations.service import EvaluationService
from app.llm.errors import LLMUnavailableError
from app.llm.fake_provider import FakeProvider, FakeToolCall, FakeTurn
from app.llm.types import CompletionRequest, CompletionResponse, StreamEvent, Usage
from app.prompts.schemas import CreatePromptInput, CreateVersionInput
from app.prompts.service import PromptService
from app.workers.tasks import run_evaluation_task
from tests.conftest import enable_builtin_tool
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

_VERDICT_CORRECT = '{"correctness": "correct", "grounded": true, "rationale": "matches"}'


# ---------------------------------------------------------------------------
# Stub providers
# ---------------------------------------------------------------------------


class _AgentProvider(FakeProvider):
    """`FakeProvider(turns=...)` that also counts `stream()` calls, records
    every request's system prompt, can fail chosen calls with an `LLMError`
    subclass, and can run a hook at the start of its first call."""

    def __init__(
        self,
        turns: list[FakeTurn],
        *,
        fail_on_calls: frozenset[int] = frozenset(),
        on_first_call: Callable[[], Awaitable[None]] | None = None,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(turns=turns, usage=usage)
        self.calls = 0
        self.systems: list[str | None] = []
        self._fail_on_calls = fail_on_calls
        self._on_first_call = on_first_call

    def stream(self, request: CompletionRequest):  # type: ignore[no-untyped-def]
        self.calls += 1
        call = self.calls
        self.systems.append(request.system)
        fail = call in self._fail_on_calls
        # Only a call that is not failing consumes a scripted turn.
        inner = None if fail else super().stream(request)
        hook = self._on_first_call if call == 1 else None

        async def _events():  # type: ignore[no-untyped-def]
            if hook is not None:
                await hook()
            if inner is None:
                raise LLMUnavailableError("provider unavailable")
            event: StreamEvent
            async for event in inner:
                yield event

        return _events()


class _JudgeProvider(FakeProvider):
    def __init__(self, *, raise_on_call: int | None = None, verdict: str = _VERDICT_CORRECT):
        super().__init__(script=[verdict])
        self.calls = 0
        self._raise_on_call = raise_on_call

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        self.calls += 1
        if self.calls == self._raise_on_call:
            raise LLMUnavailableError("judge unavailable")
        return await super().generate(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _case(**overrides: Any) -> CaseInput:
    fields: dict[str, Any] = {"question": "What is the return window?"}
    fields.update(overrides)
    return CaseInput(**fields)


async def _setup(
    tenant: TenantContext, cases: list[CaseInput], **agent_overrides: Any
) -> tuple[uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input(**agent_overrides))
        dataset = await EvaluationService(session, tenant).create_dataset(
            CreateDatasetInput(name=f"Dataset {uuid.uuid4().hex[:8]}")
        )
    case_ids: list[uuid.UUID] = []
    for case in cases:
        # One transaction per case -- see the module docstring.
        async with tenant_session(tenant) as session:
            created = await EvaluationService(session, tenant).create_case(dataset.id, case)
            case_ids.append(created.id)
    return agent.id, dataset.id, case_ids


async def _start(
    tenant: TenantContext, dataset_id: uuid.UUID, agent_id: uuid.UUID, **overrides: Any
) -> uuid.UUID:
    async with tenant_session(tenant) as session:
        run = await EvaluationService(session, tenant).create_run(
            StartRunInput(dataset_id=dataset_id, agent_id=agent_id, **overrides)
        )
        return run.id


async def _load(tenant: TenantContext, run_id: uuid.UUID) -> tuple[EvalRun, list[EvalResult]]:
    async with tenant_session(tenant) as session:
        service = EvaluationService(session, tenant)
        run = await service.get_run(run_id)
        results = await service.list_results(run_id)
    return run, results


async def _count(owner_connection, table: str, org_id: uuid.UUID, extra: str = "") -> int:  # type: ignore[no-untyped-def]
    result = await owner_connection.execute(
        text(f"SELECT count(*) FROM {table} WHERE organization_id = :org {extra}"),  # noqa: S608
        {"org": org_id},
    )
    await owner_connection.commit()
    count: int = result.scalar_one()
    return count


def _by_case(results: list[EvalResult], case_ids: list[uuid.UUID]) -> list[EvalResult]:
    by_id = {r.case_id: r for r in results}
    return [by_id[case_id] for case_id in case_ids]


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_three_case_dataset_runs_to_completed_with_a_summary(tenant_a):
    agent_id, dataset_id, case_ids = await _setup(
        tenant_a,
        [
            _case(question="What is the return window?", required_phrases=["30 days"]),
            _case(question="Is shipping free?", required_phrases=["free shipping"]),
            _case(question="What warranty?", required_phrases=["2 year warranty"]),
        ],
    )
    run_id = await _start(tenant_a, dataset_id, agent_id)
    provider = _AgentProvider(
        turns=[
            "You can return items within 30 days.",
            "Shipping is a flat $5.",
            "Every car carries a 2 year warranty.",
        ]
    )

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.COMPLETED
    assert run.completed_count == 3
    assert run.case_count == 3
    assert run.started_at is not None
    assert run.finished_at is not None
    ordered = _by_case(results, case_ids)
    assert [r.passed for r in ordered] == [True, False, True]
    assert ordered[0].question == "What is the return window?"
    assert ordered[0].answer == "You can return items within 30 days."
    assert ordered[1].scores["required_phrases"]["detail"]["missing"] == ["free shipping"]
    assert all(r.error is None for r in results)
    assert all(r.latency_ms is not None for r in results)

    summary = run.summary
    assert summary["passed"] == 2
    assert summary["failed"] == 1
    assert summary["errored"] == 0
    assert summary["pass_rate"] == pytest.approx(2 / 3)
    assert summary["scorers"] == {
        "required_phrases": {"mean": pytest.approx(2 / 3), "passed": 2, "applicable": 3}
    }
    # `fake-1` is priced at zero -- priced, so not null.
    assert summary["cost_usd"] is not None
    assert Decimal(summary["cost_usd"]) == 0
    assert isinstance(summary["mean_latency_ms"], int)


async def test_cost_is_summed_across_cases_as_a_string(tenant_a):
    agent_id, dataset_id, _ = await _setup(
        tenant_a,
        [_case(required_phrases=["a"]), _case(required_phrases=["b"])],
        model="gpt-4o-mini",
    )
    run_id = await _start(tenant_a, dataset_id, agent_id)
    provider = _AgentProvider(turns=["a", "b"], usage=Usage(input_tokens=1000, output_tokens=500))

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    run, results = await _load(tenant_a, run_id)
    # 1000 * 0.15 / 1M + 500 * 0.60 / 1M
    assert all(r.cost_usd == Decimal("0.00045") for r in results)
    assert all(r.input_tokens == 1000 and r.output_tokens == 500 for r in results)
    assert Decimal(run.summary["cost_usd"]) == Decimal("0.0009")


async def test_the_arq_task_runs_the_agents_own_provider_end_to_end(tenant_a):
    """No override anywhere: `get_provider("fake")` answers with its default
    script, the same one the offline playground shows."""
    agent_id, dataset_id, _ = await _setup(tenant_a, [_case(required_phrases=["placeholder"])])
    run_id = await _start(tenant_a, dataset_id, agent_id)

    await run_evaluation_task(
        {}, organization_id=str(tenant_a.organization_id), evaluation_run_id=str(run_id)
    )

    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.COMPLETED
    assert [r.passed for r in results] == [True]


# ---------------------------------------------------------------------------
# Review Focus 1 -- nothing a case's turn writes survives it
# ---------------------------------------------------------------------------


async def test_a_case_that_creates_a_lead_leaves_no_lead_conversation_or_message(
    tenant_a, owner_connection
):
    agent_id, dataset_id, _ = await _setup(
        tenant_a, [_case(question="I want a quote", expected_tool_names=["create_lead"])]
    )
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="create_lead")
    run_id = await _start(tenant_a, dataset_id, agent_id)
    provider = _AgentProvider(
        turns=[
            [
                FakeToolCall(
                    name="create_lead",
                    input={
                        "name": "Eval Visitor",
                        "email": "eval-visitor@example.com",
                        "interest": "a quote",
                    },
                )
            ],
            "Thanks, someone will be in touch.",
        ]
    )

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    org = tenant_a.organization_id
    assert await _count(owner_connection, "leads", org) == 0
    assert await _count(owner_connection, "conversations", org) == 0
    assert await _count(owner_connection, "messages", org) == 0
    assert await _count(owner_connection, "message_tool_calls", org) == 0
    assert await _count(owner_connection, "message_citations", org) == 0

    run, [result] = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.COMPLETED
    [call] = result.tool_calls
    assert call["name"] == "create_lead"
    assert call["is_error"] is False
    assert call["arguments"]["email"] == "eval-visitor@example.com"
    assert call["excerpt"]
    assert result.scores["tool_selection"]["passed"] is True
    assert result.passed is True
    assert result.answer == "Thanks, someone will be in touch."


async def test_usage_events_get_one_row_per_case_plus_one_per_judged_case(
    tenant_a, owner_connection
):
    agent_id, dataset_id, _ = await _setup(
        tenant_a,
        [
            _case(reference_answer="30 days"),
            _case(required_phrases=["x"]),
            _case(reference_answer="Two years"),
        ],
    )
    run_id = await _start(
        tenant_a,
        dataset_id,
        agent_id,
        judge_provider="fake",
        judge_model="judge-model-x",
    )
    judge = _JudgeProvider()

    await run_evaluation(
        tenant_a,
        run_id,
        provider_override=_AgentProvider(turns=["30 days", "x", "Two years"]),
        judge_provider_override=judge,
    )

    org = tenant_a.organization_id
    assert judge.calls == 2
    assert await _count(owner_connection, "usage_events", org) == 5
    assert await _count(owner_connection, "usage_events", org, "AND conversation_id IS NULL") == 5
    assert (
        await _count(
            owner_connection,
            "usage_events",
            org,
            f"AND model = 'judge-model-x' AND provider = 'fake' AND agent_id = '{agent_id}'",
        )
        == 2
    )
    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.COMPLETED
    judged = [r for r in results if "judge" in r.scores]
    assert len(judged) == 2
    assert all(r.scores["judge"]["status"] == "scored" for r in judged)
    # The judge model is unpriced, so a judged case's cost is unknown -- and
    # so is the run's.
    assert all(r.cost_usd is None for r in judged)
    assert run.summary["cost_usd"] is None


# ---------------------------------------------------------------------------
# Review Focus 2 -- the pinned prompt version holds for the whole run
# ---------------------------------------------------------------------------


async def test_a_version_activated_after_start_does_not_change_the_run(tenant_a):
    agent_id, dataset_id, _ = await _setup(
        tenant_a, [_case(required_phrases=["a"]), _case(required_phrases=["b"])]
    )
    async with tenant_session(tenant_a) as session:
        prompts = PromptService(session, tenant_a)
        prompt = await prompts.create_prompt(
            CreatePromptInput(
                name="Sales", key=f"sales_{uuid.uuid4().hex[:8]}", system_prompt="PINNED-V1 text"
            )
        )
        v1 = await prompts.active_version(prompt.id)
        v2 = await prompts.create_version(
            prompt.id, CreateVersionInput(system_prompt="LATER-V2 text")
        )
        agent = await AgentService(session, tenant_a).get_agent(agent_id)
        agent.prompt_id = prompt.id
    run_id = await _start(tenant_a, dataset_id, agent_id)
    async with tenant_session(tenant_a) as session:
        await PromptService(session, tenant_a).activate_version(v2.id)
    provider = _AgentProvider(turns=["a", "b"])

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    run, results = await _load(tenant_a, run_id)
    assert run.prompt_version_id == v1.id
    assert len(results) == 2
    assert all(r.prompt_version_id == v1.id for r in results)
    assert len(provider.systems) == 2
    assert all(s is not None and "PINNED-V1" in s for s in provider.systems)
    assert not any(s is not None and "LATER-V2" in s for s in provider.systems)


# ---------------------------------------------------------------------------
# Review Focus 3 -- a retried run resumes rather than re-running
# ---------------------------------------------------------------------------


async def test_a_retry_skips_cases_that_already_hold_a_result(tenant_a, owner_connection):
    agent_id, dataset_id, case_ids = await _setup(
        tenant_a,
        [
            _case(question="first", required_phrases=["one"]),
            _case(question="second", required_phrases=["two"]),
            _case(question="third", required_phrases=["three"]),
        ],
    )
    run_id = await _start(tenant_a, dataset_id, agent_id)
    # What a crashed first attempt leaves behind: the run `running`, one
    # case already recorded.
    await owner_connection.execute(
        text(
            "UPDATE eval_runs SET status = 'running', started_at = now(), completed_count = 1 "
            "WHERE id = :id"
        ),
        {"id": run_id},
    )
    await owner_connection.execute(
        text(
            "INSERT INTO eval_results "
            "(id, organization_id, run_id, case_id, question, answer, passed) "
            "VALUES (:id, :org, :run_id, :case_id, 'first', 'one', true)"
        ),
        {
            "id": uuid7(),
            "org": tenant_a.organization_id,
            "run_id": run_id,
            "case_id": case_ids[0],
        },
    )
    await owner_connection.commit()
    provider = _AgentProvider(turns=["two", "three"])

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    assert provider.calls == 2
    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.COMPLETED
    assert len(results) == 3
    assert run.completed_count == 3
    assert all(r.error is None for r in results)
    assert run.summary["passed"] == 3
    # Only the two cases actually run were billed.
    assert await _count(owner_connection, "usage_events", tenant_a.organization_id) == 2


async def test_rerunning_a_completed_run_does_nothing(tenant_a):
    agent_id, dataset_id, _ = await _setup(tenant_a, [_case(required_phrases=["a"])])
    run_id = await _start(tenant_a, dataset_id, agent_id)
    await run_evaluation(tenant_a, run_id, provider_override=_AgentProvider(turns=["a"]))
    before, _ = await _load(tenant_a, run_id)

    provider = _AgentProvider(turns=[])
    await run_evaluation(tenant_a, run_id, provider_override=provider)

    after, results = await _load(tenant_a, run_id)
    assert provider.calls == 0
    assert len(results) == 1
    assert after.finished_at == before.finished_at


# ---------------------------------------------------------------------------
# Review Focus 4 -- a judge that raises fails its case, not the run
# ---------------------------------------------------------------------------


async def test_a_judge_that_raises_fails_only_its_case(tenant_a):
    agent_id, dataset_id, case_ids = await _setup(
        tenant_a,
        [
            _case(reference_answer="30 days"),
            _case(reference_answer="Two years"),
            _case(reference_answer="Blue"),
        ],
    )
    run_id = await _start(
        tenant_a, dataset_id, agent_id, judge_provider="fake", judge_model="fake-1"
    )

    await run_evaluation(
        tenant_a,
        run_id,
        provider_override=_AgentProvider(turns=["30 days", "Two years", "Blue"]),
        judge_provider_override=_JudgeProvider(raise_on_call=2),
    )

    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.COMPLETED
    first, second, third = _by_case(results, case_ids)
    assert second.passed is False
    assert second.scores["judge"]["status"] == "error"
    assert second.scores["judge"]["score"] is None
    assert first.passed is True and first.scores["judge"]["status"] == "scored"
    assert third.passed is True and third.scores["judge"]["status"] == "scored"
    assert run.summary["scorers"]["judge"] == {"mean": 1.0, "passed": 2, "applicable": 3}


# ---------------------------------------------------------------------------
# Turn errors, cancellation, retrieval, tenancy, run failure
# ---------------------------------------------------------------------------


async def test_a_turn_error_records_the_error_and_the_run_continues(tenant_a):
    agent_id, dataset_id, case_ids = await _setup(
        tenant_a,
        [
            _case(required_phrases=["a"]),
            _case(required_phrases=["b"]),
            _case(required_phrases=["c"]),
        ],
    )
    run_id = await _start(tenant_a, dataset_id, agent_id)
    provider = _AgentProvider(turns=["a", "c"], fail_on_calls=frozenset({2}))

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.COMPLETED
    first, second, third = _by_case(results, case_ids)
    assert second.error is not None
    assert "provider unavailable" in second.error
    assert second.passed is False
    assert first.passed is True and third.passed is True
    assert run.summary["errored"] == 1
    assert run.summary["passed"] == 2
    # An errored turn reported no usage, so it has no cost figure -- but it
    # spent nothing either, so the run's total is still known.
    assert second.cost_usd is None
    assert run.summary["cost_usd"] is not None


async def test_cancelling_between_cases_stops_the_run_and_stays_cancelled(tenant_a):
    agent_id, dataset_id, _ = await _setup(
        tenant_a,
        [
            _case(required_phrases=["a"]),
            _case(required_phrases=["b"]),
            _case(required_phrases=["c"]),
        ],
    )
    run_id = await _start(tenant_a, dataset_id, agent_id)

    async def _cancel() -> None:
        # An independent session: commits on its own, whatever happens to
        # the case's rolled-back transaction.
        async with tenant_session(tenant_a) as session:
            await EvaluationService(session, tenant_a).cancel_run(run_id)

    provider = _AgentProvider(turns=["a", "b", "c"], on_first_call=_cancel)

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.CANCELLED
    assert run.finished_at is not None
    assert len(results) < 3
    assert provider.calls == 1
    assert run.summary == {}


async def test_cancel_run_leaves_a_terminal_run_unchanged(tenant_a):
    agent_id, dataset_id, _ = await _setup(tenant_a, [_case(required_phrases=["a"])])
    run_id = await _start(tenant_a, dataset_id, agent_id)
    await run_evaluation(tenant_a, run_id, provider_override=_AgentProvider(turns=["a"]))

    async with tenant_session(tenant_a) as session:
        run = await EvaluationService(session, tenant_a).cancel_run(run_id)
    assert run.status is EvalRunStatus.COMPLETED


async def test_retrieval_citations_are_recorded_and_scored(tenant_a, owner_connection):
    query = "annual maintenance inspection checklist"
    [vector] = await HashingEmbedder().embed([query])
    async with tenant_session(tenant_a) as session:
        documents = DocumentService(session, tenant_a)
        document = await documents.create(
            CreateDocumentInput(title="Maintenance Guide", source_type=DocumentSourceType.TEXT)
        )
        await documents.replace_chunks(
            document.id,
            [
                ChunkInput(
                    content=f"{query} details.",
                    token_count=5,
                    embedding=vector,
                    embedding_model="hashing",
                )
            ],
        )
        await documents.mark_ready(document.id)
        document_id = document.id
    agent_id, dataset_id, _ = await _setup(
        tenant_a,
        [
            _case(
                question="What is on the checklist?",
                expected_document_ids=[document_id],
                expected_tool_names=["retrieve_knowledge"],
            )
        ],
    )
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, tool_name="retrieve_knowledge")
    run_id = await _start(tenant_a, dataset_id, agent_id)
    provider = _AgentProvider(
        turns=[
            [FakeToolCall(name="retrieve_knowledge", input={"query": query})],
            "The checklist covers the annual inspection.",
        ]
    )

    await run_evaluation(tenant_a, run_id, provider_override=provider)

    _, [result] = await _load(tenant_a, run_id)
    assert document_id in result.cited_document_ids
    assert result.scores["document_recall"]["passed"] is True
    assert result.scores["tool_selection"]["passed"] is True
    assert result.passed is True
    assert await _count(owner_connection, "message_citations", tenant_a.organization_id) == 0


async def test_another_tenant_cannot_run_or_touch_a_run(tenant_a, tenant_b):
    agent_id, dataset_id, _ = await _setup(tenant_a, [_case(required_phrases=["a"])])
    run_id = await _start(tenant_a, dataset_id, agent_id)
    provider = _AgentProvider(turns=["a"])

    with pytest.raises(NotFoundError):
        await run_evaluation(tenant_b, run_id, provider_override=provider)

    run, results = await _load(tenant_a, run_id)
    assert provider.calls == 0
    assert run.status is EvalRunStatus.PENDING
    assert run.error is None
    assert results == []


async def test_a_failure_outside_any_case_fails_the_run_and_reraises(tenant_a, monkeypatch):
    agent_id, dataset_id, _ = await _setup(tenant_a, [_case(required_phrases=["a"])])
    run_id = await _start(tenant_a, dataset_id, agent_id)

    async def _boom(self, run_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("database went away")

    monkeypatch.setattr(runner_module.EvaluationService, "recorded_case_ids", _boom)

    with pytest.raises(RuntimeError):
        await run_evaluation(tenant_a, run_id, provider_override=_AgentProvider(turns=["a"]))

    run, results = await _load(tenant_a, run_id)
    assert run.status is EvalRunStatus.FAILED
    assert run.error is not None
    assert "RuntimeError" in run.error
    assert run.finished_at is not None
    assert results == []


async def test_recording_the_same_case_twice_is_a_no_op_not_an_error(tenant_a, owner_connection):
    """A retry racing an attempt that already recorded this case: `ON
    CONFLICT (run_id, case_id) DO NOTHING`, and no second bill."""
    from app.conversations.schemas import RecordUsageInput
    from app.db.models import UsageKind
    from app.evaluations.service import RecordResultInput

    agent_id, dataset_id, [case_id] = await _setup(tenant_a, [_case(required_phrases=["a"])])
    run_id = await _start(tenant_a, dataset_id, agent_id)

    def _input() -> RecordResultInput:
        return RecordResultInput(
            run_id=run_id,
            case_id=case_id,
            question="q",
            answer="a",
            error=None,
            scores={},
            passed=True,
            tool_calls=[],
            cited_document_ids=[],
            cited_product_ids=[],
            prompt_version_id=None,
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            cost_usd=Decimal(0),
            usage=[
                RecordUsageInput(
                    agent_id=agent_id, kind=UsageKind.LLM, provider="fake", model="fake-1"
                )
            ],
        )

    async with tenant_session(tenant_a) as session:
        first = await EvaluationService(session, tenant_a).record_result(_input())
    async with tenant_session(tenant_a) as session:
        second = await EvaluationService(session, tenant_a).record_result(_input())

    assert (first, second) == (True, False)
    run, results = await _load(tenant_a, run_id)
    assert len(results) == 1
    assert run.completed_count == 1
    assert await _count(owner_connection, "usage_events", tenant_a.organization_id) == 1
