"""`POST /api/v1/evaluations/runs` -- Task 4's REST door into a run
(docs/PHASE-6.md §5, §7).

Every test overrides `evaluations_api.enqueue_evaluation_run` with a
recorder, the same idiom `test_product_import.py` uses for
`enqueue_product_import`: the production wrapper opens a real Redis pool,
and no test in this suite may touch the network. The recorder is also what
proves §7's ordering -- at the moment it is called, the run row must already
be visible to an independent session, i.e. committed.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.agents.service import AgentService
from app.api import evaluations as evaluations_api
from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import EvalRun, EvalRunStatus, MembershipRole
from app.evaluations.schemas import CaseInput, CreateDatasetInput
from app.evaluations.service import EvaluationService
from app.main import create_app
from app.prompts.schemas import CreatePromptInput
from app.prompts.service import PromptService
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

RUNS_URL = "/api/v1/evaluations/runs"


class _RecordingQueue:
    """Records each call and, at call time, whether the run it names is
    already visible from an independent session -- i.e. committed."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.visible_at_call: list[bool] = []

    async def __call__(self, run_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self.calls.append((run_id, organization_id))
        async with tenant_session(_tenant(organization_id)) as session:
            row = await session.get(EvalRun, run_id)
        self.visible_at_call.append(row is not None)


@pytest.fixture
def queue(monkeypatch) -> _RecordingQueue:
    recorder = _RecordingQueue()
    monkeypatch.setattr(evaluations_api, "enqueue_evaluation_run", recorder)
    return recorder


@pytest.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(api_client: AsyncClient, email: str, org_name: str) -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Evals Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )


async def _org(api_client: AsyncClient, email: str, name: str) -> tuple[str, uuid.UUID]:
    token = await _register(api_client, email, name)
    return token, await _organization_id(api_client, token)


async def _agent_and_dataset(org_id: uuid.UUID, *, cases: int = 1) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input())
        service = EvaluationService(session, tenant)
        dataset = await service.create_dataset(CreateDatasetInput(name="Pricing FAQ"))
        for index in range(cases):
            await service.create_case(
                dataset.id,
                CaseInput(question=f"Question {index}?", required_phrases=["yes"]),
            )
    return agent.id, dataset.id


def _body(dataset_id: uuid.UUID, agent_id: uuid.UUID, **extra: object) -> dict[str, object]:
    return {"dataset_id": str(dataset_id), "agent_id": str(agent_id), **extra}


async def test_starting_a_run_returns_202_and_enqueues_after_commit(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "start@example.com", "Ada Motors Evals Start")
    agent_id, dataset_id = await _agent_and_dataset(org_id, cases=2)

    response = await api_client.post(
        RUNS_URL, headers=_auth(token), json=_body(dataset_id, agent_id)
    )

    assert response.status_code == 202, response.text
    body = response.json()
    run_id = uuid.UUID(body["id"])
    assert body["status"] == "pending"
    assert body["case_count"] == 2
    assert body["completed_count"] == 0
    assert body["provider"] == "fake"
    assert body["model"] == "fake-1"
    assert body["prompt_version_id"] is None
    assert queue.calls == [(run_id, org_id)]
    assert queue.visible_at_call == [True]

    async with tenant_session(_tenant(org_id)) as session:
        run = await EvaluationService(session, _tenant(org_id)).get_run(run_id)
    assert run.status is EvalRunStatus.PENDING
    assert run.triggered_by is not None


async def test_the_agents_active_prompt_version_is_pinned_at_start(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "pin@example.com", "Ada Motors Evals Pin")
    agent_id, dataset_id = await _agent_and_dataset(org_id)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        prompts = PromptService(session, tenant)
        prompt = await prompts.create_prompt(
            CreatePromptInput(name="Sales", key="sales", system_prompt="Hello")
        )
        version = await prompts.active_version(prompt.id)
        agent = await AgentService(session, tenant).get_agent(agent_id)
        agent.prompt_id = prompt.id

    response = await api_client.post(
        RUNS_URL,
        headers=_auth(token),
        json=_body(dataset_id, agent_id, provider="fake", model="fake-2"),
    )

    assert response.status_code == 202, response.text
    assert response.json()["prompt_version_id"] == str(version.id)
    assert response.json()["model"] == "fake-2"


async def test_a_second_active_run_of_the_same_dataset_is_409(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "twice@example.com", "Ada Motors Evals Twice")
    agent_id, dataset_id = await _agent_and_dataset(org_id)

    first = await api_client.post(RUNS_URL, headers=_auth(token), json=_body(dataset_id, agent_id))
    second = await api_client.post(RUNS_URL, headers=_auth(token), json=_body(dataset_id, agent_id))

    assert first.status_code == 202, first.text
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "conflict"
    assert len(queue.calls) == 1


async def test_an_empty_dataset_is_422(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "empty@example.com", "Ada Motors Evals Empty")
    agent_id, dataset_id = await _agent_and_dataset(org_id, cases=0)

    response = await api_client.post(
        RUNS_URL, headers=_auth(token), json=_body(dataset_id, agent_id)
    )

    assert response.status_code == 422, response.text
    assert queue.calls == []


async def test_a_dataset_over_the_case_limit_is_422(
    api_client, clean_users, queue, owner_connection
):
    token, org_id = await _org(api_client, "big@example.com", "Ada Motors Evals Big")
    agent_id, dataset_id = await _agent_and_dataset(org_id, cases=0)
    # Past what `create_case` would ever allow -- inserted directly.
    for index in range(201):
        await owner_connection.execute(
            text(
                "INSERT INTO eval_cases (id, organization_id, dataset_id, question, "
                "required_phrases, expected_tool_names, expected_document_ids, "
                "expected_product_ids, tags) "
                "VALUES (:id, :org, :dataset_id, :question, ARRAY['yes'], '{}', '{}', '{}', '{}')"
            ),
            {"id": uuid7(), "org": org_id, "dataset_id": dataset_id, "question": f"Q{index}"},
        )
    await owner_connection.commit()

    response = await api_client.post(
        RUNS_URL, headers=_auth(token), json=_body(dataset_id, agent_id)
    )

    assert response.status_code == 422, response.text
    assert queue.calls == []


async def test_a_prompt_version_of_another_prompt_is_422(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "otherp@example.com", "Ada Motors Evals OtherPrompt")
    agent_id, dataset_id = await _agent_and_dataset(org_id)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        prompts = PromptService(session, tenant)
        agents_prompt = await prompts.create_prompt(
            CreatePromptInput(name="Mine", key="mine", system_prompt="Mine")
        )
        other_prompt = await prompts.create_prompt(
            CreatePromptInput(name="Other", key="other", system_prompt="Other")
        )
        other_version = await prompts.active_version(other_prompt.id)
        agent = await AgentService(session, tenant).get_agent(agent_id)
        agent.prompt_id = agents_prompt.id

    response = await api_client.post(
        RUNS_URL,
        headers=_auth(token),
        json=_body(dataset_id, agent_id, prompt_version_id=str(other_version.id)),
    )

    assert response.status_code == 422, response.text
    assert queue.calls == []


async def test_a_judge_provider_without_a_judge_model_is_422(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "judge@example.com", "Ada Motors Evals Judge")
    agent_id, dataset_id = await _agent_and_dataset(org_id)

    response = await api_client.post(
        RUNS_URL,
        headers=_auth(token),
        json=_body(dataset_id, agent_id, judge_provider="fake"),
    )

    assert response.status_code == 422, response.text
    assert queue.calls == []


async def test_a_provider_without_a_model_is_422(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "prov@example.com", "Ada Motors Evals Provider")
    agent_id, dataset_id = await _agent_and_dataset(org_id)

    response = await api_client.post(
        RUNS_URL, headers=_auth(token), json=_body(dataset_id, agent_id, provider="fake")
    )

    assert response.status_code == 422, response.text


async def test_an_unknown_provider_is_422(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "unknown@example.com", "Ada Motors Evals Unknown")
    agent_id, dataset_id = await _agent_and_dataset(org_id)

    response = await api_client.post(
        RUNS_URL,
        headers=_auth(token),
        json=_body(dataset_id, agent_id, judge_provider="nope", judge_model="x"),
    )

    assert response.status_code == 422, response.text
    assert queue.calls == []


async def test_another_orgs_dataset_is_404(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "mine@example.com", "Ada Motors Evals Mine")
    _, other_org_id = await _org(api_client, "theirs@example.com", "Ada Motors Evals Theirs")
    agent_id, _ = await _agent_and_dataset(org_id)
    _, their_dataset_id = await _agent_and_dataset(other_org_id)

    response = await api_client.post(
        RUNS_URL, headers=_auth(token), json=_body(their_dataset_id, agent_id)
    )

    assert response.status_code == 404, response.text
    assert queue.calls == []


async def test_another_orgs_agent_is_404(api_client, clean_users, queue):
    token, org_id = await _org(api_client, "mine2@example.com", "Ada Motors Evals Mine2")
    _, other_org_id = await _org(api_client, "theirs2@example.com", "Ada Motors Evals Theirs2")
    _, dataset_id = await _agent_and_dataset(org_id)
    their_agent_id, _ = await _agent_and_dataset(other_org_id)

    response = await api_client.post(
        RUNS_URL, headers=_auth(token), json=_body(dataset_id, their_agent_id)
    )

    assert response.status_code == 404, response.text
    assert queue.calls == []


async def test_starting_a_run_without_a_token_is_401(api_client, clean_users, queue):
    response = await api_client.post(RUNS_URL, json=_body(uuid.uuid4(), uuid.uuid4()))

    assert response.status_code == 401, response.text
    assert queue.calls == []


async def test_a_failed_enqueue_fails_the_run_instead_of_leaving_it_pending(
    api_client, clean_users, monkeypatch
):
    """The run is committed before the enqueue (§7), so an enqueue that
    fails -- Redis down -- would otherwise leave a `pending` run no worker
    will ever pick up, and that dataset 409-locked behind it."""
    token, org_id = await _org(api_client, "redis@example.com", "Ada Motors Evals Redis")
    agent_id, dataset_id = await _agent_and_dataset(org_id)

    async def _down(run_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(evaluations_api, "enqueue_evaluation_run", _down)

    with pytest.raises(ConnectionError):
        await api_client.post(RUNS_URL, headers=_auth(token), json=_body(dataset_id, agent_id))

    async with tenant_session(_tenant(org_id)) as session:
        [run] = await EvaluationService(session, _tenant(org_id)).list_runs(dataset_id)
    assert run.status is EvalRunStatus.FAILED
    assert run.error is not None
