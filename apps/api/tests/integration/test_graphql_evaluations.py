"""The GraphQL surface for Phase 6 evaluations (Task 5, docs/PHASE-6.md §7).

Starting a run stays REST (`test_evaluations_api.py`) -- everything else
(dataset/case CRUD, listing runs and results, cancelling) is here, following
`test_graphql_products.py`'s idiom: rows are seeded directly through
`EvaluationService`/`AgentService`/`PromptService`, and this file is only
about what the GraphQL read/write surface returns.
"""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.agents.service import AgentService
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole
from app.evaluations.schemas import CaseInput, CreateDatasetInput
from app.evaluations.service import EvaluationService, RecordResultInput
from app.main import create_app
from app.prompts.schemas import CreatePromptInput
from app.prompts.service import PromptService
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _clean(clean_users) -> None:
    return None


@pytest.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


async def _register(api_client: AsyncClient, email: str, org_name: str = "Ada Motors") -> str:
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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


def _tenant(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )


async def _org(
    api_client: AsyncClient, email: str, name: str = "Ada Motors"
) -> tuple[str, uuid.UUID]:
    token = await _register(api_client, email, name)
    return token, await _organization_id(api_client, token)


async def _dataset(org_id: uuid.UUID, name: str = "Pricing FAQ", **overrides: object) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        dataset = await EvaluationService(session, tenant).create_dataset(
            CreateDatasetInput(name=name, **overrides)
        )
    return dataset.id


async def _case(org_id: uuid.UUID, dataset_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    tenant = _tenant(org_id)
    fields: dict[str, object] = {"question": "What is the return policy?"}
    fields.update(overrides)
    fields.setdefault("required_phrases", ["30 days"])
    async with tenant_session(tenant) as session:
        case = await EvaluationService(session, tenant).create_case(
            dataset_id,
            CaseInput(**fields),  # type: ignore[arg-type]
        )
    return case.id


async def _agent(org_id: uuid.UUID, name: str = "Sales Bot") -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input(name=name))
    return agent.id


async def _run(
    org_id: uuid.UUID,
    dataset_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    prompt_version_id: uuid.UUID | None = None,
) -> uuid.UUID:
    from app.evaluations.schemas import StartRunInput

    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        run = await EvaluationService(session, tenant).create_run(
            StartRunInput(
                dataset_id=dataset_id,
                agent_id=agent_id,
                prompt_version_id=prompt_version_id,
            )
        )
    return run.id


async def _record_result(
    org_id: uuid.UUID, run_id: uuid.UUID, case_id: uuid.UUID, **overrides: object
) -> None:
    tenant = _tenant(org_id)
    fields: dict[str, object] = {
        "run_id": run_id,
        "case_id": case_id,
        "question": "q",
        "answer": "a",
        "error": None,
        "scores": {},
        "passed": True,
        "tool_calls": [],
        "cited_document_ids": [],
        "cited_product_ids": [],
        "prompt_version_id": None,
        "latency_ms": 120,
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": Decimal("0.0001"),
    }
    fields.update(overrides)
    async with tenant_session(tenant) as session:
        await EvaluationService(session, tenant).record_result(RecordResultInput(**fields))  # type: ignore[arg-type]


async def _complete_run(org_id: uuid.UUID, run_id: uuid.UUID) -> None:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        service = EvaluationService(session, tenant)
        await service.claim_run(run_id)
        await service.complete_run(run_id)


# ---------------------------------------------------------------------------
# evaluationDatasets / evaluationDataset
# ---------------------------------------------------------------------------

DATASETS_QUERY = """
query {
  evaluationDatasets {
    id name description caseCount createdAt updatedAt
    latestRun { id status }
  }
}
"""


async def test_evaluation_datasets_requires_authentication(api_client):
    response = await graphql(api_client, DATASETS_QUERY)

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_evaluation_datasets_lists_the_callers_datasets_with_case_count(api_client):
    token, org_id = await _org(api_client, "list-datasets@example.com")
    dataset_id = await _dataset(org_id, "Pricing FAQ", description="core questions")
    await _case(org_id, dataset_id)
    await _case(org_id, dataset_id, question="Second question?", required_phrases=["ok"])

    response = await graphql(api_client, DATASETS_QUERY, headers=_auth(token))

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["evaluationDatasets"]
    assert row["id"] == str(dataset_id)
    assert row["name"] == "Pricing FAQ"
    assert row["description"] == "core questions"
    assert row["caseCount"] == 2
    assert row["latestRun"] is None


async def test_evaluation_datasets_is_scoped_to_the_callers_org(api_client):
    token_a, org_a = await _org(api_client, "datasets-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "datasets-b@example.com", "Ada Motors B")
    mine = await _dataset(org_a, "Org A Dataset")
    await _dataset(org_b, "Org B Dataset")

    response = await graphql(api_client, DATASETS_QUERY, headers=_auth(token_a))

    rows = response.json()["data"]["evaluationDatasets"]
    assert [row["id"] for row in rows] == [str(mine)]


async def test_evaluation_datasets_latest_run_is_the_most_recent(api_client):
    token, org_id = await _org(api_client, "latest-run@example.com")
    dataset_id = await _dataset(org_id)
    await _case(org_id, dataset_id)
    agent_id = await _agent(org_id)
    first_run = await _run(org_id, dataset_id, agent_id)
    await _complete_run(org_id, first_run)
    # cancel_run / create_run both refuse a second active run of the same
    # dataset, so completing the first before starting the second is required
    # to prove "latest" rather than "only".
    second_run = await _run(org_id, dataset_id, agent_id)

    response = await graphql(api_client, DATASETS_QUERY, headers=_auth(token))

    [row] = response.json()["data"]["evaluationDatasets"]
    assert row["latestRun"]["id"] == str(second_run)
    assert row["latestRun"]["status"] == "PENDING"


DATASET_QUERY = "query D($id: UUID!) { evaluationDataset(id: $id) { id name } }"


async def test_evaluation_dataset_requires_authentication(api_client):
    response = await graphql(api_client, DATASET_QUERY, {"id": str(uuid.uuid4())})

    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_evaluation_dataset_another_orgs_id_is_not_found(api_client):
    token_a, _org_a = await _org(api_client, "dataset-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "dataset-nf-b@example.com", "Ada Motors B")
    theirs = await _dataset(org_b, "Their Dataset")

    response = await graphql(api_client, DATASET_QUERY, {"id": str(theirs)}, _auth(token_a))

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_evaluation_dataset_returns_the_callers_own(api_client):
    token, org_id = await _org(api_client, "dataset-own@example.com")
    dataset_id = await _dataset(org_id, "Mine")

    response = await graphql(api_client, DATASET_QUERY, {"id": str(dataset_id)}, _auth(token))

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["evaluationDataset"]["name"] == "Mine"


async def test_evaluation_datasets_case_count_and_latest_run_are_batched(api_client):
    """The N+1 `caseCount`/`latestRun` dataloaders exist to prevent."""
    from sqlalchemy import event

    from app.db.session import engine

    token, org_id = await _org(api_client, "datasets-batching@example.com")
    agent_id = await _agent(org_id)
    dataset_ids = []
    for index in range(3):
        dataset_id = await _dataset(org_id, f"Dataset {index}")
        await _case(org_id, dataset_id)
        run_id = await _run(org_id, dataset_id, agent_id)
        await _complete_run(org_id, run_id)
        dataset_ids.append(dataset_id)

    case_statements: list[str] = []
    run_statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        upper = statement.lstrip().upper()
        if not upper.startswith("SELECT"):
            return
        if "eval_cases" in statement:
            case_statements.append(statement)
        if "eval_runs" in statement:
            run_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await graphql(api_client, DATASETS_QUERY, headers=_auth(token))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    body = response.json()
    assert "errors" not in body, body
    assert len(body["data"]["evaluationDatasets"]) == 3
    assert len(case_statements) == 1, f"expected one batched query, got {len(case_statements)}"
    assert len(run_statements) == 1, f"expected one batched query, got {len(run_statements)}"


# ---------------------------------------------------------------------------
# evaluationCases
# ---------------------------------------------------------------------------

CASES_QUERY = """
query C($datasetId: UUID!) {
  evaluationCases(datasetId: $datasetId) {
    id question referenceAnswer requiredPhrases expectedToolNames
    expectedDocumentIds expectedProductIds tags createdAt updatedAt
  }
}
"""


async def test_evaluation_cases_requires_authentication(api_client):
    response = await graphql(api_client, CASES_QUERY, {"datasetId": str(uuid.uuid4())})

    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_evaluation_cases_lists_a_datasets_cases(api_client):
    token, org_id = await _org(api_client, "cases@example.com")
    dataset_id = await _dataset(org_id)
    case_id = await _case(
        org_id,
        dataset_id,
        reference_answer="30 days, no questions asked.",
        required_phrases=["30 days"],
        expected_tool_names=["retrieve_knowledge"],
        tags=["returns"],
    )

    response = await graphql(api_client, CASES_QUERY, {"datasetId": str(dataset_id)}, _auth(token))

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["evaluationCases"]
    assert row["id"] == str(case_id)
    assert row["question"] == "What is the return policy?"
    assert row["referenceAnswer"] == "30 days, no questions asked."
    assert row["requiredPhrases"] == ["30 days"]
    assert row["expectedToolNames"] == ["retrieve_knowledge"]
    assert row["expectedDocumentIds"] == []
    assert row["expectedProductIds"] == []
    assert row["tags"] == ["returns"]


async def test_evaluation_cases_another_orgs_dataset_is_not_found(api_client):
    token_a, _org_a = await _org(api_client, "cases-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "cases-nf-b@example.com", "Ada Motors B")
    theirs = await _dataset(org_b)

    response = await graphql(api_client, CASES_QUERY, {"datasetId": str(theirs)}, _auth(token_a))

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Dataset CRUD mutations
# ---------------------------------------------------------------------------


async def test_create_evaluation_dataset(api_client):
    token, _org_id = await _org(api_client, "create-dataset@example.com")

    response = await graphql(
        api_client,
        """
        mutation {
          createEvaluationDataset(input: {name: "New Dataset", description: "desc"}) {
            id name description caseCount
          }
        }
        """,
        headers=_auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    created = body["data"]["createEvaluationDataset"]
    assert created["name"] == "New Dataset"
    assert created["description"] == "desc"
    assert created["caseCount"] == 0


async def test_create_evaluation_dataset_invalid_input_is_invalid_input(api_client):
    """Validation errors from the pydantic schema (blank name) surface as
    `invalid_input` through `_build`, not a raw pydantic message."""
    token, _org_id = await _org(api_client, "create-dataset-invalid@example.com")

    response = await graphql(
        api_client,
        'mutation { createEvaluationDataset(input: {name: "   "}) { id } }',
        headers=_auth(token),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "invalid_input"


async def test_update_evaluation_dataset(api_client):
    token, org_id = await _org(api_client, "update-dataset@example.com")
    dataset_id = await _dataset(org_id, "Old Name")

    response = await graphql(
        api_client,
        """
        mutation U($id: UUID!) {
          updateEvaluationDataset(id: $id, input: {name: "New Name"}) { id name }
        }
        """,
        {"id": str(dataset_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["updateEvaluationDataset"]["name"] == "New Name"


async def test_update_evaluation_dataset_another_orgs_id_is_not_found(api_client):
    token_a, _org_a = await _org(api_client, "update-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "update-nf-b@example.com", "Ada Motors B")
    theirs = await _dataset(org_b)

    response = await graphql(
        api_client,
        """
        mutation U($id: UUID!) {
          updateEvaluationDataset(id: $id, input: {name: "Hijacked"}) { id }
        }
        """,
        {"id": str(theirs)},
        _auth(token_a),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_delete_evaluation_dataset(api_client):
    token, org_id = await _org(api_client, "delete-dataset@example.com")
    dataset_id = await _dataset(org_id)

    response = await graphql(
        api_client,
        "mutation D($id: UUID!) { deleteEvaluationDataset(id: $id) }",
        {"id": str(dataset_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["deleteEvaluationDataset"] is True

    check = await graphql(api_client, DATASETS_QUERY, headers=_auth(token))
    assert check.json()["data"]["evaluationDatasets"] == []


async def test_delete_evaluation_dataset_another_orgs_id_is_not_found(api_client):
    token_a, _org_a = await _org(api_client, "delete-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "delete-nf-b@example.com", "Ada Motors B")
    theirs = await _dataset(org_b)

    response = await graphql(
        api_client,
        "mutation D($id: UUID!) { deleteEvaluationDataset(id: $id) }",
        {"id": str(theirs)},
        _auth(token_a),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Case CRUD mutations
# ---------------------------------------------------------------------------


async def test_create_evaluation_case(api_client):
    token, org_id = await _org(api_client, "create-case@example.com")
    dataset_id = await _dataset(org_id)

    response = await graphql(
        api_client,
        """
        mutation C($datasetId: UUID!) {
          createEvaluationCase(
            datasetId: $datasetId
            input: {question: "How much?", requiredPhrases: ["$40"]}
          ) { id question requiredPhrases }
        }
        """,
        {"datasetId": str(dataset_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    created = body["data"]["createEvaluationCase"]
    assert created["question"] == "How much?"
    assert created["requiredPhrases"] == ["$40"]


async def test_create_evaluation_case_without_any_expectation_is_invalid_input(api_client):
    token, org_id = await _org(api_client, "create-case-invalid@example.com")
    dataset_id = await _dataset(org_id)

    response = await graphql(
        api_client,
        """
        mutation C($datasetId: UUID!) {
          createEvaluationCase(datasetId: $datasetId, input: {question: "How much?"}) { id }
        }
        """,
        {"datasetId": str(dataset_id)},
        _auth(token),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "invalid_input"


async def test_create_evaluation_case_another_orgs_dataset_is_not_found(api_client):
    token_a, _org_a = await _org(api_client, "create-case-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "create-case-nf-b@example.com", "Ada Motors B")
    theirs = await _dataset(org_b)

    response = await graphql(
        api_client,
        """
        mutation C($datasetId: UUID!) {
          createEvaluationCase(
            datasetId: $datasetId
            input: {question: "?", requiredPhrases: ["x"]}
          ) { id }
        }
        """,
        {"datasetId": str(theirs)},
        _auth(token_a),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_update_evaluation_case(api_client):
    token, org_id = await _org(api_client, "update-case@example.com")
    dataset_id = await _dataset(org_id)
    case_id = await _case(org_id, dataset_id)

    response = await graphql(
        api_client,
        """
        mutation U($id: UUID!) {
          updateEvaluationCase(
            id: $id
            input: {question: "Updated?", requiredPhrases: ["updated"]}
          ) { id question requiredPhrases }
        }
        """,
        {"id": str(case_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    updated = body["data"]["updateEvaluationCase"]
    assert updated["question"] == "Updated?"
    assert updated["requiredPhrases"] == ["updated"]


async def test_update_evaluation_case_another_orgs_id_is_not_found(api_client):
    token_a, org_a = await _org(api_client, "update-case-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "update-case-nf-b@example.com", "Ada Motors B")
    theirs_dataset = await _dataset(org_b)
    theirs_case = await _case(org_b, theirs_dataset)
    _ = org_a

    response = await graphql(
        api_client,
        """
        mutation U($id: UUID!) {
          updateEvaluationCase(
            id: $id
            input: {question: "Hijacked", requiredPhrases: ["x"]}
          ) { id }
        }
        """,
        {"id": str(theirs_case)},
        _auth(token_a),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_delete_evaluation_case(api_client):
    token, org_id = await _org(api_client, "delete-case@example.com")
    dataset_id = await _dataset(org_id)
    case_id = await _case(org_id, dataset_id)

    response = await graphql(
        api_client,
        "mutation D($id: UUID!) { deleteEvaluationCase(id: $id) }",
        {"id": str(case_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["deleteEvaluationCase"] is True

    check = await graphql(api_client, CASES_QUERY, {"datasetId": str(dataset_id)}, _auth(token))
    assert check.json()["data"]["evaluationCases"] == []


async def test_delete_evaluation_case_another_orgs_id_is_not_found(api_client):
    token_a, _org_a = await _org(api_client, "delete-case-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "delete-case-nf-b@example.com", "Ada Motors B")
    theirs_dataset = await _dataset(org_b)
    theirs_case = await _case(org_b, theirs_dataset)

    response = await graphql(
        api_client,
        "mutation D($id: UUID!) { deleteEvaluationCase(id: $id) }",
        {"id": str(theirs_case)},
        _auth(token_a),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# evaluationRuns / evaluationRun
# ---------------------------------------------------------------------------

RUNS_QUERY = """
query R($datasetId: UUID, $limit: Int) {
  evaluationRuns(datasetId: $datasetId, limit: $limit) {
    id datasetId agentId agentName promptVersionId promptVersion
    provider model judgeProvider judgeModel status caseCount completedCount
    error triggeredBy createdAt startedAt finishedAt
    summary { passed failed errored passRate costUsd meanLatencyMs
              scorers { name mean passed applicable } }
  }
}
"""


async def test_evaluation_runs_requires_authentication(api_client):
    response = await graphql(api_client, RUNS_QUERY)

    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_evaluation_runs_lists_runs_with_agent_name_and_no_prompt_version(api_client):
    token, org_id = await _org(api_client, "runs-list@example.com")
    dataset_id = await _dataset(org_id)
    await _case(org_id, dataset_id)
    agent_id = await _agent(org_id, "Sales Bot")
    run_id = await _run(org_id, dataset_id, agent_id)

    response = await graphql(api_client, RUNS_QUERY, headers=_auth(token))

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["evaluationRuns"]
    assert row["id"] == str(run_id)
    assert row["datasetId"] == str(dataset_id)
    assert row["agentId"] == str(agent_id)
    assert row["agentName"] == "Sales Bot"
    assert row["promptVersionId"] is None
    assert row["promptVersion"] is None
    assert row["provider"] == "fake"
    assert row["model"] == "fake-1"
    assert row["status"] == "PENDING"
    assert row["caseCount"] == 1
    assert row["completedCount"] == 0
    assert row["summary"] is None


async def test_evaluation_runs_summary_is_null_until_the_run_has_one(api_client):
    token, org_id = await _org(api_client, "runs-no-summary@example.com")
    dataset_id = await _dataset(org_id)
    await _case(org_id, dataset_id)
    agent_id = await _agent(org_id)
    run_id = await _run(org_id, dataset_id, agent_id)

    response = await graphql(api_client, RUNS_QUERY, headers=_auth(token))

    [row] = response.json()["data"]["evaluationRuns"]
    assert row["id"] == str(run_id)
    assert row["summary"] is None


async def test_evaluation_runs_promotes_prompt_version_number(api_client):
    token, org_id = await _org(api_client, "runs-promptver@example.com")
    dataset_id = await _dataset(org_id)
    await _case(org_id, dataset_id)
    agent_id = await _agent(org_id)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        prompts = PromptService(session, tenant)
        prompt = await prompts.create_prompt(
            CreatePromptInput(name="Sales", key="sales", system_prompt="Hello")
        )
        version = await prompts.active_version(prompt.id)
        agent = await AgentService(session, tenant).get_agent(agent_id)
        agent.prompt_id = prompt.id
    run_id = await _run(org_id, dataset_id, agent_id)

    response = await graphql(api_client, RUNS_QUERY, headers=_auth(token))

    [row] = response.json()["data"]["evaluationRuns"]
    assert row["id"] == str(run_id)
    assert row["promptVersionId"] == str(version.id)
    assert row["promptVersion"] == version.version == 1


async def test_evaluation_runs_summary_reports_typed_fields_in_scorer_order(api_client):
    token, org_id = await _org(api_client, "runs-summary@example.com")
    dataset_id = await _dataset(org_id)
    case_1 = await _case(org_id, dataset_id, question="Q1", required_phrases=["ok"])
    case_2 = await _case(
        org_id, dataset_id, question="Q2", expected_tool_names=["retrieve_knowledge"]
    )
    agent_id = await _agent(org_id)
    run_id = await _run(org_id, dataset_id, agent_id)
    # Inserted deliberately out of spec order to prove the GraphQL layer
    # sorts by the fixed scorer order rather than trusting dict order.
    await _record_result(
        org_id,
        run_id,
        case_1,
        scores={
            "tool_selection": {"score": 1.0, "passed": True, "status": "scored", "detail": {}},
            "required_phrases": {"score": 1.0, "passed": True, "status": "scored", "detail": {}},
        },
        passed=True,
    )
    await _record_result(
        org_id,
        run_id,
        case_2,
        scores={
            "tool_selection": {"score": 0.0, "passed": False, "status": "scored", "detail": {}}
        },
        passed=False,
    )
    await _complete_run(org_id, run_id)

    response = await graphql(api_client, RUNS_QUERY, headers=_auth(token))

    [row] = response.json()["data"]["evaluationRuns"]
    assert row["status"] == "COMPLETED"
    summary = row["summary"]
    assert (summary["passed"], summary["failed"], summary["errored"]) == (1, 1, 0)
    assert summary["passRate"] == 0.5
    # `Numeric(12, 6)` keeps its full precision as text -- two results at
    # 0.000100 each.
    assert summary["costUsd"] == "0.000200"
    assert summary["meanLatencyMs"] == 120
    # judge is absent (no judge configured); document_recall/product_recall
    # absent (no expectation) -- only required_phrases, tool_selection appear,
    # in that fixed order, not insertion order.
    assert [s["name"] for s in summary["scorers"]] == ["required_phrases", "tool_selection"]
    tool_selection = next(s for s in summary["scorers"] if s["name"] == "tool_selection")
    assert tool_selection["mean"] == 0.5
    assert tool_selection["passed"] == 1
    assert tool_selection["applicable"] == 2


async def test_evaluation_runs_is_scoped_to_the_callers_org(api_client):
    token_a, org_a = await _org(api_client, "runs-org-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "runs-org-b@example.com", "Ada Motors B")
    dataset_a = await _dataset(org_a)
    await _case(org_a, dataset_a)
    agent_a = await _agent(org_a)
    mine = await _run(org_a, dataset_a, agent_a)
    dataset_b = await _dataset(org_b)
    await _case(org_b, dataset_b)
    agent_b = await _agent(org_b)
    await _run(org_b, dataset_b, agent_b)

    response = await graphql(api_client, RUNS_QUERY, headers=_auth(token_a))

    rows = response.json()["data"]["evaluationRuns"]
    assert [row["id"] for row in rows] == [str(mine)]


async def test_evaluation_runs_filters_and_limits_by_dataset(api_client):
    token, org_id = await _org(api_client, "runs-filter@example.com")
    agent_id = await _agent(org_id)
    dataset_a = await _dataset(org_id, "A")
    await _case(org_id, dataset_a)
    run_a = await _run(org_id, dataset_a, agent_id)
    dataset_b = await _dataset(org_id, "B")
    await _case(org_id, dataset_b)
    await _run(org_id, dataset_b, agent_id)

    response = await graphql(api_client, RUNS_QUERY, {"datasetId": str(dataset_a)}, _auth(token))

    rows = response.json()["data"]["evaluationRuns"]
    assert [row["id"] for row in rows] == [str(run_a)]


async def test_evaluation_runs_with_another_orgs_dataset_id_is_empty(api_client):
    """Unlike `evaluationCases`, `list_runs` filters `dataset_id` without an
    ownership pre-check -- a foreign dataset id simply matches nothing scoped
    to this organization, so the result is an empty list, not a `not_found`
    error."""
    token_a, _org_a = await _org(api_client, "runs-empty-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "runs-empty-b@example.com", "Ada Motors B")
    theirs = await _dataset(org_b)

    response = await graphql(api_client, RUNS_QUERY, {"datasetId": str(theirs)}, _auth(token_a))

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["evaluationRuns"] == []


async def test_evaluation_runs_batches_agent_name_and_prompt_version(api_client):
    """The N+1 `agentName`/`promptVersion` dataloaders exist to prevent."""
    from sqlalchemy import event

    from app.db.session import engine

    token, org_id = await _org(api_client, "runs-batching@example.com")
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        prompts = PromptService(session, tenant)
        prompt = await prompts.create_prompt(
            CreatePromptInput(name="Sales", key="sales", system_prompt="Hello")
        )
    for index in range(3):
        agent_id = await _agent(org_id, f"Bot {index}")
        async with tenant_session(tenant) as session:
            agent = await AgentService(session, tenant).get_agent(agent_id)
            agent.prompt_id = prompt.id
        dataset_id = await _dataset(org_id, f"Dataset {index}")
        await _case(org_id, dataset_id)
        await _run(org_id, dataset_id, agent_id)

    agent_statements: list[str] = []
    version_statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        upper = statement.lstrip().upper()
        if not upper.startswith("SELECT"):
            return
        if "FROM agents" in statement:
            agent_statements.append(statement)
        if "prompt_versions" in statement:
            version_statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await graphql(api_client, RUNS_QUERY, headers=_auth(token))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    body = response.json()
    assert "errors" not in body, body
    assert len(body["data"]["evaluationRuns"]) == 3
    assert len(agent_statements) == 1, f"expected one batched query, got {len(agent_statements)}"
    assert len(version_statements) == 1, (
        f"expected one batched query, got {len(version_statements)}"
    )


RUN_QUERY = """
query R($id: UUID!) {
  evaluationRun(id: $id) {
    id status
    results {
      id caseId question answer error passed
      scores { name score passed status detail }
      toolCalls { name arguments isError excerpt }
      citedDocumentIds citedProductIds promptVersionId
      latencyMs inputTokens outputTokens costUsd createdAt
    }
  }
}
"""


async def test_evaluation_run_requires_authentication(api_client):
    response = await graphql(api_client, RUN_QUERY, {"id": str(uuid.uuid4())})

    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_evaluation_run_another_orgs_id_is_null(api_client):
    token_a, _org_a = await _org(api_client, "run-null-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "run-null-b@example.com", "Ada Motors B")
    dataset_b = await _dataset(org_b)
    await _case(org_b, dataset_b)
    agent_b = await _agent(org_b)
    theirs = await _run(org_b, dataset_b, agent_b)

    response = await graphql(api_client, RUN_QUERY, {"id": str(theirs)}, _auth(token_a))

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["evaluationRun"] is None


async def test_evaluation_run_returns_results_with_scores_and_tool_calls(api_client):
    token, org_id = await _org(api_client, "run-results@example.com")
    dataset_id = await _dataset(org_id)
    case_id = await _case(
        org_id, dataset_id, required_phrases=["ok"], expected_tool_names=["retrieve_knowledge"]
    )
    agent_id = await _agent(org_id)
    run_id = await _run(org_id, dataset_id, agent_id)
    await _record_result(
        org_id,
        run_id,
        case_id,
        question="What is the return policy?",
        answer="30 days, ok.",
        scores={
            "tool_selection": {
                "score": 1.0,
                "passed": True,
                "status": "scored",
                "detail": {"missing": [], "unexpected": []},
            },
            "judge": {
                "score": 1.0,
                "passed": True,
                "status": "scored",
                "detail": {"rationale": "correct"},
            },
            "required_phrases": {
                "score": 1.0,
                "passed": True,
                "status": "scored",
                "detail": {"missing": []},
            },
        },
        passed=True,
        tool_calls=[
            {
                "name": "retrieve_knowledge",
                "arguments": {"query": "return policy"},
                "is_error": False,
                "excerpt": "Returns are accepted within 30 days.",
            }
        ],
        cost_usd=Decimal("0.000123"),
    )

    response = await graphql(api_client, RUN_QUERY, {"id": str(run_id)}, _auth(token))

    body = response.json()
    assert "errors" not in body, body
    run = body["data"]["evaluationRun"]
    assert run["id"] == str(run_id)
    [result] = run["results"]
    assert result["caseId"] == str(case_id)
    assert result["question"] == "What is the return policy?"
    assert result["answer"] == "30 days, ok."
    assert result["passed"] is True
    # Fixed order: judge, required_phrases, tool_selection, document_recall,
    # product_recall -- not insertion order (inserted above as tool_selection,
    # judge, required_phrases).
    assert [s["name"] for s in result["scores"]] == [
        "judge",
        "required_phrases",
        "tool_selection",
    ]
    import json as _json

    judge = result["scores"][0]
    assert judge["score"] == 1.0
    assert judge["passed"] is True
    assert judge["status"] == "scored"
    assert _json.loads(judge["detail"]) == {"rationale": "correct"}
    [tool_call] = result["toolCalls"]
    assert tool_call["name"] == "retrieve_knowledge"
    assert _json.loads(tool_call["arguments"]) == {"query": "return policy"}
    assert tool_call["isError"] is False
    assert tool_call["excerpt"] == "Returns are accepted within 30 days."
    assert result["costUsd"] == "0.000123"


async def test_evaluation_runs_list_does_not_load_results(api_client):
    """`results` resolves lazily per field -- listing runs must not touch
    `eval_results` at all."""
    from sqlalchemy import event

    from app.db.session import engine

    token, org_id = await _org(api_client, "runs-lazy@example.com")
    dataset_id = await _dataset(org_id)
    case_id = await _case(org_id, dataset_id)
    agent_id = await _agent(org_id)
    run_id = await _run(org_id, dataset_id, agent_id)
    await _record_result(org_id, run_id, case_id)

    statements: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        if "eval_results" in statement:
            statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        response = await graphql(api_client, RUNS_QUERY, headers=_auth(token))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    body = response.json()
    assert "errors" not in body, body
    assert len(body["data"]["evaluationRuns"]) == 1
    assert statements == []


# ---------------------------------------------------------------------------
# cancelEvaluationRun
# ---------------------------------------------------------------------------


async def test_cancel_evaluation_run(api_client):
    token, org_id = await _org(api_client, "cancel-run@example.com")
    dataset_id = await _dataset(org_id)
    await _case(org_id, dataset_id)
    agent_id = await _agent(org_id)
    run_id = await _run(org_id, dataset_id, agent_id)

    response = await graphql(
        api_client,
        "mutation Cancel($id: UUID!) { cancelEvaluationRun(id: $id) { id status } }",
        {"id": str(run_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["cancelEvaluationRun"]["status"] == "CANCELLED"


async def test_cancel_evaluation_run_is_idempotent_on_a_terminal_run(api_client):
    token, org_id = await _org(api_client, "cancel-idempotent@example.com")
    dataset_id = await _dataset(org_id)
    case_id = await _case(org_id, dataset_id)
    agent_id = await _agent(org_id)
    run_id = await _run(org_id, dataset_id, agent_id)
    await _record_result(org_id, run_id, case_id)
    await _complete_run(org_id, run_id)

    response = await graphql(
        api_client,
        "mutation Cancel($id: UUID!) { cancelEvaluationRun(id: $id) { id status } }",
        {"id": str(run_id)},
        _auth(token),
    )

    body = response.json()
    assert "errors" not in body, body
    # Still completed -- a terminal run is returned unchanged, not flipped
    # to cancelled.
    assert body["data"]["cancelEvaluationRun"]["status"] == "COMPLETED"


async def test_cancel_evaluation_run_another_orgs_id_is_not_found(api_client):
    token_a, _org_a = await _org(api_client, "cancel-nf-a@example.com", "Ada Motors A")
    _token_b, org_b = await _org(api_client, "cancel-nf-b@example.com", "Ada Motors B")
    dataset_b = await _dataset(org_b)
    await _case(org_b, dataset_b)
    agent_b = await _agent(org_b)
    theirs = await _run(org_b, dataset_b, agent_b)

    response = await graphql(
        api_client,
        "mutation Cancel($id: UUID!) { cancelEvaluationRun(id: $id) { id } }",
        {"id": str(theirs)},
        _auth(token_a),
    )

    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"
