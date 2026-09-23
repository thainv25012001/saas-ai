import uuid

import pytest
from sqlalchemy import text

from app.agents.service import AgentService
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import uuid7
from app.core.tenancy import tenant_session
from app.db.models import DocumentSourceType
from app.documents.schemas import CreateDocumentInput
from app.documents.service import DocumentService
from app.evaluations.schemas import (
    MAX_CASES_PER_DATASET,
    CaseInput,
    CreateDatasetInput,
    UpdateDatasetInput,
)
from app.evaluations.service import EvaluationService
from app.products.schemas import ProductInput
from app.products.service import ProductService
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def _dataset(session, tenant, name: str = "Pricing FAQ", **overrides):
    return await EvaluationService(session, tenant).create_dataset(
        CreateDatasetInput(name=name, **overrides)
    )


def _case_input(**overrides: object) -> CaseInput:
    fields: dict[str, object] = {
        "question": "What is the return policy?",
        "required_phrases": ["30 days"],
    }
    fields.update(overrides)
    return CaseInput(**fields)  # type: ignore[arg-type]


async def _insert_run(
    owner_connection,
    tenant,
    dataset_id,
    agent_id,
    *,
    status: str = "completed",
) -> uuid.UUID:
    run_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO eval_runs "
            "(id, organization_id, dataset_id, agent_id, provider, model, status, "
            "case_count, completed_count) "
            "VALUES (:id, :org, :dataset_id, :agent_id, 'fake', 'fake-1', :status, 1, 1)"
        ),
        {
            "id": run_id,
            "org": tenant.organization_id,
            "dataset_id": dataset_id,
            "agent_id": agent_id,
            "status": status,
        },
    )
    await owner_connection.commit()
    return run_id


async def _insert_result(owner_connection, tenant, run_id) -> uuid.UUID:
    result_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO eval_results (id, organization_id, run_id, question, answer, passed) "
            "VALUES (:id, :org, :run_id, 'q', 'a', true)"
        ),
        {"id": result_id, "org": tenant.organization_id, "run_id": run_id},
    )
    await owner_connection.commit()
    return result_id


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


async def test_create_dataset_returns_a_dataset_scoped_to_the_tenant(tenant_a):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a, description="Common questions")
    assert dataset.organization_id == tenant_a.organization_id
    assert dataset.name == "Pricing FAQ"
    assert dataset.description == "Common questions"


async def test_list_datasets_returns_only_this_tenants_rows(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        await _dataset(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        await _dataset(session, tenant_b, name="Tenant B's dataset")

    async with tenant_session(tenant_a) as session:
        [listed] = await EvaluationService(session, tenant_a).list_datasets()
    assert listed.name == "Pricing FAQ"


async def test_update_dataset_renames_it(tenant_a):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        updated = await EvaluationService(session, tenant_a).update_dataset(
            dataset.id, UpdateDatasetInput(name="Renamed", description="new")
        )
    assert updated.name == "Renamed"
    assert updated.description == "new"


async def test_delete_dataset_removes_the_row(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        await EvaluationService(session, tenant_a).delete_dataset(dataset.id)

    result = await owner_connection.execute(
        text("SELECT COUNT(*) FROM eval_datasets WHERE id = :id"), {"id": dataset.id}
    )
    assert result.scalar_one() == 0


async def test_delete_dataset_cascades_its_cases(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        case = await EvaluationService(session, tenant_a).create_case(dataset.id, _case_input())

    async with tenant_session(tenant_a) as session:
        await EvaluationService(session, tenant_a).delete_dataset(dataset.id)

    result = await owner_connection.execute(
        text("SELECT COUNT(*) FROM eval_cases WHERE id = :id"), {"id": case.id}
    )
    assert result.scalar_one() == 0


async def test_create_dataset_with_duplicate_name_raises_conflict(tenant_a):
    async with tenant_session(tenant_a) as session:
        await _dataset(session, tenant_a)
        with pytest.raises(ConflictError):
            await _dataset(session, tenant_a)


async def test_create_dataset_same_name_different_tenant_is_allowed(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        await _dataset(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        dataset_b = await _dataset(session, tenant_b)
    assert dataset_b.name == "Pricing FAQ"


async def test_get_dataset_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await EvaluationService(session, tenant_b).get_dataset(dataset.id)


async def test_create_dataset_conflicting_with_a_concurrently_inserted_row_raises_conflict(
    tenant_a, owner_connection
):
    """`create_dataset` has no SELECT-then-INSERT pre-check -- it relies
    solely on `uq_eval_dataset_org_name` plus a `try/except IntegrityError`
    around the flush (same pattern as `AgentService.create_agent`). This
    inserts the conflicting row through a wholly separate connection, after
    this test's own session has already started, to prove the conflict is
    caught even when nothing in this session's own history could have seen
    it coming -- the scenario a pre-check would silently race under."""
    async with tenant_session(tenant_a) as session:
        # A "concurrent" writer -- a different connection entirely -- claims
        # the name first.
        await owner_connection.execute(
            text("INSERT INTO eval_datasets (id, organization_id, name) VALUES (:id, :org, :name)"),
            {"id": uuid7(), "org": tenant_a.organization_id, "name": "Pricing FAQ"},
        )
        await owner_connection.commit()

        with pytest.raises(ConflictError):
            await EvaluationService(session, tenant_a).create_dataset(
                CreateDatasetInput(name="Pricing FAQ")
            )


async def test_update_dataset_rename_to_an_existing_name_raises_conflict(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = EvaluationService(session, tenant_a)
        await service.create_dataset(CreateDatasetInput(name="Dataset A"))
        dataset_b = await service.create_dataset(CreateDatasetInput(name="Dataset B"))

        with pytest.raises(ConflictError):
            await service.update_dataset(dataset_b.id, UpdateDatasetInput(name="Dataset A"))


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


async def test_create_case_requires_at_least_one_expectation():
    with pytest.raises(ValueError):
        CaseInput(question="What colour is the sky?")


async def test_create_list_update_delete_case(tenant_a):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        service = EvaluationService(session, tenant_a)
        case = await service.create_case(dataset.id, _case_input())
        assert case.dataset_id == dataset.id
        assert case.required_phrases == ["30 days"]

        [listed] = await service.list_cases(dataset.id)
        assert listed.id == case.id
        assert await service.count_cases(dataset.id) == 1

        updated = await service.update_case(
            case.id, _case_input(question="Updated question?", required_phrases=["updated"])
        )
        assert updated.question == "Updated question?"
        assert updated.required_phrases == ["updated"]

        await service.delete_case(case.id)
        assert await service.list_cases(dataset.id) == []


async def test_list_cases_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await EvaluationService(session, tenant_b).list_cases(dataset.id)


async def test_create_case_referencing_another_orgs_document_id_is_rejected_without_echoing_it(
    tenant_a, tenant_b
):
    async with tenant_session(tenant_b) as session:
        other_org_document = await DocumentService(session, tenant_b).create(
            CreateDocumentInput(title="Org B doc", source_type=DocumentSourceType.TEXT)
        )

    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        with pytest.raises(ValidationError) as excinfo:
            await EvaluationService(session, tenant_a).create_case(
                dataset.id,
                _case_input(
                    required_phrases=[],
                    expected_document_ids=[other_org_document.id],
                ),
            )
    assert str(other_org_document.id) not in str(excinfo.value)
    assert "1" in str(excinfo.value)


async def test_create_case_referencing_own_orgs_document_id_succeeds(tenant_a):
    async with tenant_session(tenant_a) as session:
        document = await DocumentService(session, tenant_a).create(
            CreateDocumentInput(title="Our doc", source_type=DocumentSourceType.TEXT)
        )
        dataset = await _dataset(session, tenant_a)
        case = await EvaluationService(session, tenant_a).create_case(
            dataset.id,
            _case_input(required_phrases=[], expected_document_ids=[document.id]),
        )
    assert case.expected_document_ids == [document.id]


async def test_create_case_referencing_another_orgs_product_id_is_rejected(tenant_a, tenant_b):
    async with tenant_session(tenant_b) as session:
        other_org_product = await ProductService(session, tenant_b).create(
            ProductInput(external_id="sku-1", name="Widget", slug="widget")
        )

    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        with pytest.raises(ValidationError):
            await EvaluationService(session, tenant_a).create_case(
                dataset.id,
                _case_input(
                    required_phrases=[],
                    expected_product_ids=[other_org_product.id],
                ),
            )


async def test_create_case_with_unknown_tool_name_is_rejected():
    with pytest.raises(ValueError):
        _case_input(required_phrases=[], expected_tool_names=["not_a_real_tool"])


async def test_create_case_with_a_known_tool_name_succeeds(tenant_a):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        case = await EvaluationService(session, tenant_a).create_case(
            dataset.id,
            _case_input(required_phrases=[], expected_tool_names=["retrieve_knowledge"]),
        )
    assert case.expected_tool_names == ["retrieve_knowledge"]


async def test_201st_case_in_a_dataset_is_rejected(tenant_a):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        service = EvaluationService(session, tenant_a)
        for _ in range(MAX_CASES_PER_DATASET):
            await service.create_case(dataset.id, _case_input())

        with pytest.raises(ValidationError):
            await service.create_case(dataset.id, _case_input())


async def test_update_case_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        case = await EvaluationService(session, tenant_a).create_case(dataset.id, _case_input())

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await EvaluationService(session, tenant_b).update_case(case.id, _case_input())


async def test_delete_case_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        case = await EvaluationService(session, tenant_a).create_case(dataset.id, _case_input())

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await EvaluationService(session, tenant_b).delete_case(case.id)


# ---------------------------------------------------------------------------
# Runs / results (read-only in Task 1 -- rows are inserted directly, since
# `create_run` does not exist until Task 4).
# ---------------------------------------------------------------------------


async def test_list_runs_and_get_run(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        agent = await AgentService(session, tenant_a).create_agent(agent_input())

    run_id = await _insert_run(owner_connection, tenant_a, dataset.id, agent.id)

    async with tenant_session(tenant_a) as session:
        service = EvaluationService(session, tenant_a)
        [listed] = await service.list_runs(dataset.id)
        assert listed.id == run_id

        run = await service.get_run(run_id)
        assert run.id == run_id
        assert run.dataset_id == dataset.id


async def test_get_run_from_another_tenant_raises_not_found(tenant_a, tenant_b, owner_connection):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        agent = await AgentService(session, tenant_a).create_agent(agent_input())
    run_id = await _insert_run(owner_connection, tenant_a, dataset.id, agent.id)

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await EvaluationService(session, tenant_b).get_run(run_id)


async def test_list_results_ordered_by_created_at(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        agent = await AgentService(session, tenant_a).create_agent(agent_input())
    run_id = await _insert_run(owner_connection, tenant_a, dataset.id, agent.id)
    first = await _insert_result(owner_connection, tenant_a, run_id)
    second = await _insert_result(owner_connection, tenant_a, run_id)

    async with tenant_session(tenant_a) as session:
        results = await EvaluationService(session, tenant_a).list_results(run_id)
    assert [result.id for result in results] == [first, second]


async def test_list_results_from_another_tenants_run_raises_not_found(
    tenant_a, tenant_b, owner_connection
):
    async with tenant_session(tenant_a) as session:
        dataset = await _dataset(session, tenant_a)
        agent = await AgentService(session, tenant_a).create_agent(agent_input())
    run_id = await _insert_run(owner_connection, tenant_a, dataset.id, agent.id)

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await EvaluationService(session, tenant_b).list_results(run_id)
