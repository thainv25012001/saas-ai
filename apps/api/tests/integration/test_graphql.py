import pytest
from sqlalchemy import event

from app.agents.service import AgentService
from app.db.session import engine as db_engine

pytestmark = pytest.mark.anyio

REGISTRATION = {
    "email": "gql@example.com",
    "password": "correct-horse-battery",
    "full_name": "Grace GraphQL",
    "organization_name": "Ada Motors GQL",
}


@pytest.fixture
async def auth_headers(client, clean_users):
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


async def test_graphql_requires_authentication(client):
    response = await graphql(client, "{ agents { id } }")
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_me_returns_the_current_user_and_org(client, auth_headers):
    response = await graphql(
        client,
        "{ me { email organizationName role } }",
        headers=auth_headers,
    )
    assert response.json()["data"]["me"] == {
        "email": "gql@example.com",
        "organizationName": "Ada Motors GQL",
        "role": "owner",
    }


async def test_organization_returns_the_callers_own_org(client, auth_headers):
    response = await graphql(
        client,
        "{ organization { id name slug plan createdAt } }",
        headers=auth_headers,
    )
    organization = response.json()["data"]["organization"]
    assert organization["name"] == "Ada Motors GQL"
    assert organization["plan"] == "free"
    assert organization["slug"]
    assert organization["createdAt"]

    me = await graphql(client, "{ me { organizationId } }", headers=auth_headers)
    assert organization["id"] == me.json()["data"]["me"]["organizationId"]


async def test_organization_requires_authentication(client):
    response = await graphql(client, "{ organization { name } }")
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_organization_takes_no_id_argument(client, auth_headers):
    """Scoping is a server-side decision: there is deliberately no way for a
    caller to name which organization it wants."""
    response = await graphql(
        client,
        'query { organization(id: "00000000-0000-0000-0000-000000000000") { name } }',
        headers=auth_headers,
    )
    assert response.json()["errors"], "an id argument must not be accepted"


async def test_create_agent_mutation(client, auth_headers):
    response = await graphql(
        client,
        """
        mutation Create($name: String!) {
          createAgent(input: {name: $name}) { id name slug status }
        }
        """,
        {"name": "Showroom Bot"},
        auth_headers,
    )
    agent = response.json()["data"]["createAgent"]
    assert agent["slug"] == "showroom-bot"
    assert agent["status"] == "DRAFT"


async def test_create_agent_mutation_resolves_provider_and_model_from_settings(
    client, auth_headers
):
    """Regression test: the GraphQL input type used to declare its own
    `provider: str = "openai"` default and pass it straight through, which
    short-circuited `AgentService.create_agent`'s `data.provider or
    default_llm_provider` resolution to a hardcoded "openai" for every agent
    created through the dashboard — the only path a real user exercises.
    `model` has the same failure mode: it must resolve to the *chosen*
    provider's own default model, not a hardcoded OpenAI model string paired
    with (e.g.) the `fake` provider."""
    from app.core.config import get_settings
    from app.llm.registry import DEFAULT_MODELS

    response = await graphql(
        client,
        'mutation { createAgent(input: {name: "Default Provider Bot"}) { provider model } }',
        headers=auth_headers,
    )
    agent = response.json()["data"]["createAgent"]
    default_provider = get_settings().default_llm_provider
    assert agent["provider"] == default_provider
    assert agent["model"] == DEFAULT_MODELS[default_provider]


async def test_agents_query_lists_created_agents(client, auth_headers):
    await graphql(
        client,
        'mutation { createAgent(input: {name: "Listed Bot"}) { id } }',
        headers=auth_headers,
    )
    response = await graphql(client, "{ agents { name } }", headers=auth_headers)
    names = [a["name"] for a in response.json()["data"]["agents"]]
    assert "Listed Bot" in names


async def test_agent_query_includes_its_config(client, auth_headers):
    created = await graphql(
        client,
        'mutation { createAgent(input: {name: "Config Bot"}) { id } }',
        headers=auth_headers,
    )
    agent_id = created.json()["data"]["createAgent"]["id"]
    response = await graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name config { tone retrievalTopK } } }",
        {"id": agent_id},
        auth_headers,
    )
    assert response.json()["data"]["agent"]["config"] == {
        "tone": "friendly",
        "retrievalTopK": 5,
    }


async def test_update_agent_config_mutation(client, auth_headers):
    created = await graphql(
        client,
        'mutation { createAgent(input: {name: "Tuned Bot"}) { id } }',
        headers=auth_headers,
    )
    agent_id = created.json()["data"]["createAgent"]["id"]
    response = await graphql(
        client,
        """
        mutation U($id: UUID!) {
          updateAgentConfig(agentId: $id, input: {tone: "formal"}) { tone }
        }
        """,
        {"id": agent_id},
        auth_headers,
    )
    assert response.json()["data"]["updateAgentConfig"]["tone"] == "formal"


async def test_unknown_agent_returns_a_not_found_code(client, auth_headers):
    response = await graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name } }",
        {"id": "00000000-0000-7000-8000-000000000000"},
        auth_headers,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_prompt_version_activation_through_graphql(client, auth_headers):
    created = await graphql(
        client,
        """
        mutation {
          createPrompt(input: {
            name: "Sales", key: "sales_system", systemPrompt: "v1"
          }) { id }
        }
        """,
        headers=auth_headers,
    )
    prompt_id = created.json()["data"]["createPrompt"]["id"]

    version = await graphql(
        client,
        """
        mutation V($id: UUID!) {
          createPromptVersion(promptId: $id, input: {systemPrompt: "v2"}) {
            id version isActive
          }
        }
        """,
        {"id": prompt_id},
        auth_headers,
    )
    version_id = version.json()["data"]["createPromptVersion"]["id"]
    assert version.json()["data"]["createPromptVersion"]["isActive"] is False

    activated = await graphql(
        client,
        "mutation A($id: UUID!) { activatePromptVersion(versionId: $id) { version isActive } }",
        {"id": version_id},
        auth_headers,
    )
    assert activated.json()["data"]["activatePromptVersion"] == {
        "version": 2,
        "isActive": True,
    }


async def test_unauthenticated_mutation_requires_authentication(client):
    response = await graphql(
        client,
        'mutation { createAgent(input: {name: "Nope"}) { id } }',
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_invalid_input_is_reported_as_invalid_input_not_a_raw_pydantic_message(
    client, auth_headers
):
    """`temperature` is constrained (0.0-2.0) by `agent_schemas.CreateAgentInput`,
    not by the GraphQL input type itself, so this reaches pydantic's
    `ValidationError` inside the resolver. It must come back as the app's own
    `invalid_input` code, not fall through as an unrecognised error carrying
    pydantic's raw multi-line message and its errors.pydantic.dev URL."""
    response = await graphql(
        client,
        """
        mutation Create($name: String!, $temperature: Float!) {
          createAgent(input: {name: $name, temperature: $temperature}) { id }
        }
        """,
        {"name": "Hot Bot", "temperature": 5},
        auth_headers,
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "invalid_input"
    assert "errors.pydantic.dev" not in body["errors"][0]["message"]


async def test_unexpected_errors_do_not_leak_internal_details(client, auth_headers, monkeypatch):
    """A bug, or a raw database error, must never reach the client with its
    own message - it must be replaced with a generic one and `internal_error`,
    not fall through the extension's `elif` unrecognised."""

    async def _boom(self: AgentService, *args: object, **kwargs: object) -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(AgentService, "list_agents", _boom)

    response = await graphql(client, "{ agents { id } }", headers=auth_headers)
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "internal_error"
    assert body["errors"][0]["message"] == "internal server error"
    assert "secret internal detail" not in response.text


async def test_introspection_still_works_in_the_test_environment(client):
    """Introspection is disabled outside `environment == "local"` (see
    app/graphql/schema.py). The test environment runs as `local`, so this
    must keep working - codegen (Task 11) and any future schema tooling
    depend on it."""
    response = await graphql(client, "{ __schema { types { name } } }")
    body = response.json()
    assert "errors" not in body
    type_names = [t["name"] for t in body["data"]["__schema"]["types"]]
    assert "Agent" in type_names


async def test_update_agent_mutation_round_trips_status_and_fields(client, auth_headers):
    """The only path that exercises the AgentStatus GraphQL enum's NAME
    (`ACTIVE`) converting to the ORM's VALUE (`active`) on the way in."""
    created = await graphql(
        client,
        'mutation { createAgent(input: {name: "Status Bot"}) { id } }',
        headers=auth_headers,
    )
    agent_id = created.json()["data"]["createAgent"]["id"]

    response = await graphql(
        client,
        """
        mutation U($id: UUID!) {
          updateAgent(id: $id, input: {status: ACTIVE, name: "Renamed Bot"}) {
            status
            name
            slug
          }
        }
        """,
        {"id": agent_id},
        auth_headers,
    )
    body = response.json()["data"]["updateAgent"]
    assert body == {"status": "ACTIVE", "name": "Renamed Bot", "slug": "renamed-bot"}


async def test_delete_agent_mutation_removes_it(client, auth_headers):
    created = await graphql(
        client,
        'mutation { createAgent(input: {name: "Deletable Bot"}) { id } }',
        headers=auth_headers,
    )
    agent_id = created.json()["data"]["createAgent"]["id"]

    deleted = await graphql(
        client,
        "mutation D($id: UUID!) { deleteAgent(id: $id) }",
        {"id": agent_id},
        auth_headers,
    )
    assert deleted.json()["data"]["deleteAgent"] is True

    response = await graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name } }",
        {"id": agent_id},
        auth_headers,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_prompts_query_lists_created_prompts(client, auth_headers):
    await graphql(
        client,
        """
        mutation {
          createPrompt(input: {
            name: "Support", key: "support_system", systemPrompt: "hello"
          }) { id }
        }
        """,
        headers=auth_headers,
    )
    response = await graphql(client, "{ prompts { name key } }", headers=auth_headers)
    names = [p["name"] for p in response.json()["data"]["prompts"]]
    assert "Support" in names


async def test_prompt_query_returns_a_single_prompt(client, auth_headers):
    created = await graphql(
        client,
        """
        mutation {
          createPrompt(input: {
            name: "Onboarding", key: "onboarding_system", systemPrompt: "hi"
          }) { id }
        }
        """,
        headers=auth_headers,
    )
    prompt_id = created.json()["data"]["createPrompt"]["id"]
    response = await graphql(
        client,
        "query P($id: UUID!) { prompt(id: $id) { name key } }",
        {"id": prompt_id},
        auth_headers,
    )
    assert response.json()["data"]["prompt"] == {"name": "Onboarding", "key": "onboarding_system"}


async def test_agent_config_field_batches_into_a_single_query(client, auth_headers):
    """`agents { config { ... } }` must issue one batched query for all
    agents' configs via `Context.config_loader`, not one query per agent."""
    await graphql(
        client,
        'mutation { createAgent(input: {name: "Batch One"}) { id } }',
        headers=auth_headers,
    )
    await graphql(
        client,
        'mutation { createAgent(input: {name: "Batch Two"}) { id } }',
        headers=auth_headers,
    )

    config_statements: list[str] = []

    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "agent_configs" in statement:
            config_statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        response = await graphql(
            client,
            "{ agents { name config { tone } } }",
            headers=auth_headers,
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    assert "errors" not in response.json()
    assert len(config_statements) == 1
