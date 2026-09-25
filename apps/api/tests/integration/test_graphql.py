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


async def create_agent(client, headers, name, selection="id", **fields):
    """Create an agent over GraphQL with the boilerplate filled in.

    `provider` and `model` are required by the API — deliberately, so a user
    has to choose rather than inherit `DEFAULT_LLM_PROVIDER` — and almost no
    test here cares which. Default to the offline pair, which needs no API
    key; a test that cares passes its own.
    """
    payload = {"name": name, "provider": "fake", "model": "fake-1", **fields}
    return await graphql(
        client,
        f"mutation C($input: CreateAgentInput!) {{ createAgent(input: $input) {{ {selection} }} }}",
        {"input": payload},
        headers,
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
    response = await create_agent(
        client, auth_headers, "Showroom Bot", selection="id name slug status"
    )
    agent = response.json()["data"]["createAgent"]
    assert agent["slug"] == "showroom-bot"
    assert agent["status"] == "DRAFT"


async def test_create_agent_mutation_stores_the_chosen_provider_and_model(client, auth_headers):
    response = await create_agent(
        client,
        auth_headers,
        "Chosen Bot",
        selection="provider model",
        provider="anthropic",
        model="claude-opus-5",
    )
    assert response.json()["data"]["createAgent"] == {
        "provider": "anthropic",
        "model": "claude-opus-5",
    }


async def test_agents_query_lists_created_agents(client, auth_headers):
    await create_agent(client, auth_headers, "Listed Bot")
    response = await graphql(client, "{ agents { name } }", headers=auth_headers)
    names = [a["name"] for a in response.json()["data"]["agents"]]
    assert "Listed Bot" in names


async def test_agent_query_includes_its_config(client, auth_headers):
    created = await create_agent(client, auth_headers, "Config Bot")
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
    created = await create_agent(client, auth_headers, "Tuned Bot")
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
    response = await create_agent(client, None, "Nope")
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_invalid_input_is_reported_as_invalid_input_not_a_raw_pydantic_message(
    client, auth_headers
):
    """`temperature` is constrained (0.0-2.0) by `agent_schemas.CreateAgentInput`,
    not by the GraphQL input type itself, so this reaches pydantic's
    `ValidationError` inside the resolver. It must come back as the app's own
    `invalid_input` code, not fall through as an unrecognised error carrying
    pydantic's raw multi-line message and its errors.pydantic.dev URL."""
    response = await create_agent(client, auth_headers, "Hot Bot", temperature=5)
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "invalid_input"
    assert "errors.pydantic.dev" not in body["errors"][0]["message"]


async def test_create_agent_rejects_an_unknown_provider(client, auth_headers):
    """`AgentService.create_agent` used to do
    `DEFAULT_MODELS.get(provider, DEFAULT_MODELS["openai"])`, so a typo'd or
    invented provider name was accepted and silently paired with an OpenAI
    model -- an agent that could never resolve a provider at send time. The
    name is validated against `registry.KNOWN_PROVIDERS` in the schema
    instead, so it is rejected here, at creation."""
    response = await create_agent(client, auth_headers, "Bogus Bot", provider="gpt")
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "invalid_input"
    assert "fake" in body["errors"][0]["message"]


async def test_update_agent_rejects_an_unknown_provider(client, auth_headers):
    """`update_agent` `setattr`s `provider` straight onto the row with no
    validation at all -- in contrast to the careful `AgentStatus` handling
    immediately above it. Both mutations share one schema, so both reject
    the same values the same way."""
    created = await create_agent(client, auth_headers, "Switchable Bot")
    agent_id = created.json()["data"]["createAgent"]["id"]

    response = await graphql(
        client,
        """
        mutation U($id: UUID!) {
          updateAgent(id: $id, input: {provider: "gpt"}) { provider }
        }
        """,
        {"id": agent_id},
        auth_headers,
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "invalid_input"


async def test_update_agent_accepts_every_known_provider(client, auth_headers):
    """The counterpart to the test above: the validator must not be so strict
    that it rejects the providers that do exist -- `fake` included, which is
    what a fresh clone's agents are actually created with."""
    from app.llm.registry import KNOWN_PROVIDERS

    created = await create_agent(client, auth_headers, "Every Provider Bot")
    agent_id = created.json()["data"]["createAgent"]["id"]

    for provider in KNOWN_PROVIDERS:
        response = await graphql(
            client,
            """
            mutation U($id: UUID!, $provider: String!) {
              updateAgent(id: $id, input: {provider: $provider}) { provider }
            }
            """,
            {"id": agent_id, "provider": provider},
            auth_headers,
        )
        body = response.json()
        assert "errors" not in body, body
        assert body["data"]["updateAgent"]["provider"] == provider


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
    created = await create_agent(client, auth_headers, "Status Bot")
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
    created = await create_agent(client, auth_headers, "Deletable Bot")
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
    await create_agent(client, auth_headers, "Batch One")
    await create_agent(client, auth_headers, "Batch Two")

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


async def test_provider_models_requires_authentication(client):
    response = await graphql(client, '{ providerModels(provider: "openrouter") { id } }')
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_provider_models_lists_openrouter_models(client, auth_headers, monkeypatch):
    """Monkeypatched, not live: the dashboard's dropdown must not make this
    suite depend on OpenRouter being up."""
    from app.llm import openrouter_models as om

    async def _fake_fetch():
        return {
            "data": [
                {"id": "vendor/free-one:free", "name": "Free One", "context_length": 128},
                {"id": "vendor/paid", "name": "Paid"},
            ]
        }

    om.reset_cache()
    monkeypatch.setattr(om, "_fetch_payload", _fake_fetch)

    response = await graphql(
        client,
        '{ providerModels(provider: "openrouter") { id label contextLength } }',
        headers=auth_headers,
    )
    assert response.json()["data"]["providerModels"] == [
        {"id": "vendor/free-one:free", "label": "Free One", "contextLength": 128}
    ]
    om.reset_cache()


async def test_provider_models_returns_static_options_for_other_providers(client, auth_headers):
    """OpenRouter is the only provider with a model-list API. The others still
    answer, so the dashboard has one query rather than a special case."""
    response = await graphql(
        client,
        '{ providerModels(provider: "anthropic") { id } }',
        headers=auth_headers,
    )
    ids = [m["id"] for m in response.json()["data"]["providerModels"]]
    assert "claude-opus-5" in ids


async def test_provider_models_rejects_an_unknown_provider(client, auth_headers):
    """The message assertion is load-bearing: a query naming a field that does
    not exist also errors, so without it this test passed before the resolver
    was written at all."""
    response = await graphql(
        client,
        '{ providerModels(provider: "not-a-provider") { id } }',
        headers=auth_headers,
    )
    error = response.json()["errors"][0]
    assert error["extensions"]["code"] == "invalid_input"
    assert "unknown provider 'not-a-provider'" in error["message"]


async def test_configured_providers_requires_authentication(client):
    response = await graphql(client, "{ configuredProviders { id configured } }")
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_configured_providers_lists_every_known_provider(client, auth_headers):
    """The dashboard builds its whole provider dropdown from this, so a
    provider missing here is a provider nobody can select."""
    from app.llm.registry import KNOWN_PROVIDERS

    response = await graphql(client, "{ configuredProviders { id } }", headers=auth_headers)
    body = response.json()
    assert "errors" not in body, body
    assert [p["id"] for p in body["data"]["configuredProviders"]] == list(KNOWN_PROVIDERS)


async def test_configured_providers_reports_fake_as_configured(client, auth_headers):
    response = await graphql(
        client, "{ configuredProviders { id configured } }", headers=auth_headers
    )
    by_id = {p["id"]: p["configured"] for p in response.json()["data"]["configuredProviders"]}
    assert by_id["fake"] is True


async def test_configured_providers_reflects_which_keys_are_set(client, auth_headers, monkeypatch):
    """Monkeypatched rather than read from the ambient .env: a test whose
    result depends on whether the developer happens to have an OpenAI key is
    not a test."""
    from app.core.config import get_settings

    base = get_settings()
    patched = base.model_copy(
        update={
            "openai_api_key": "test-key",
            "anthropic_api_key": None,
            "openrouter_api_key": None,
        }
    )
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: patched)

    response = await graphql(
        client, "{ configuredProviders { id configured } }", headers=auth_headers
    )
    by_id = {p["id"]: p["configured"] for p in response.json()["data"]["configuredProviders"]}
    assert by_id["openai"] is True
    assert by_id["anthropic"] is False
    assert by_id["openrouter"] is False


async def test_create_agent_input_declares_provider_and_model_non_null():
    """Non-null in the schema itself, not merely rejected by pydantic inside
    the resolver. This is what the dashboard's codegen reads: with these
    nullable, a `createAgent` call that omits them still type-checks in the
    web app and only fails at runtime, which is how every agent ended up on
    `fake` in the first place."""
    from app.graphql.schema import schema

    sdl = schema.as_str()
    block = sdl[sdl.index("input CreateAgentInput") :]
    block = block[: block.index("}")]
    assert "provider: String!" in block, block
    assert "model: String!" in block, block


async def test_create_agent_omitting_them_fails_before_execution(client, auth_headers):
    """The corollary: the request is rejected as an invalid query rather than
    reaching the resolver. `AppErrorExtension` labels both cases
    `invalid_input`, so the message is what tells them apart -- and naming the
    required field and its type is the more useful of the two for a client."""
    response = await graphql(
        client,
        'mutation { createAgent(input: {name: "Nameless Bot"}) { id } }',
        headers=auth_headers,
    )
    body = response.json()
    assert "data" not in body or body["data"] is None, body
    message = body["errors"][0]["message"]
    assert "CreateAgentInput.provider" in message, message
    assert "required type 'String!'" in message, message


async def test_create_agent_rejects_an_empty_model(client, auth_headers):
    """The dashboard's model picker starts empty, so this is exactly what an
    unfilled form sends -- it must not create an agent with no model."""
    response = await create_agent(client, auth_headers, "Modelless Bot", model="")
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "invalid_input"


async def _create_prompt(client, headers, key, *extra_versions):
    created = await graphql(
        client,
        "mutation C($key: String!) {"
        ' createPrompt(input: {name: "Sales", key: $key, systemPrompt: "v1"}) { id } }',
        {"key": key},
        headers,
    )
    prompt_id = created.json()["data"]["createPrompt"]["id"]
    for text in extra_versions:
        await graphql(
            client,
            "mutation V($id: UUID!, $t: String!) {"
            " createPromptVersion(promptId: $id, input: {systemPrompt: $t}) { id } }",
            {"id": prompt_id, "t": text},
            headers,
        )
    return prompt_id


async def test_prompt_versions_are_listed_newest_first(client, auth_headers):
    prompt_id = await _create_prompt(client, auth_headers, "versions_listed", "v2", "v3")

    response = await graphql(
        client,
        "query P($id: UUID!) { prompt(id: $id) { versions { id version isActive createdAt } } }",
        {"id": prompt_id},
        auth_headers,
    )

    body = response.json()
    assert "errors" not in body, body
    versions = body["data"]["prompt"]["versions"]
    assert [(v["version"], v["isActive"]) for v in versions] == [
        (3, False),
        (2, False),
        (1, True),
    ]


async def test_prompts_versions_batch_into_a_single_query(client, auth_headers):
    await _create_prompt(client, auth_headers, "batch_one", "v2")
    await _create_prompt(client, auth_headers, "batch_two")

    statements: list[str] = []

    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        if "FROM prompt_versions" in statement:
            statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _capture)
    try:
        response = await graphql(
            client, "{ prompts { key versions { version } } }", headers=auth_headers
        )
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _capture)

    body = response.json()
    assert "errors" not in body, body
    by_key = {p["key"]: [v["version"] for v in p["versions"]] for p in body["data"]["prompts"]}
    assert by_key["batch_one"] == [2, 1]
    assert by_key["batch_two"] == [1]
    assert len(statements) == 1


async def test_another_orgs_prompt_versions_are_not_reachable(client, auth_headers):
    theirs = await _create_prompt(client, auth_headers, "their_prompt", "v2")
    other = await client.post(
        "/api/v1/auth/register",
        json={**REGISTRATION, "email": "gql-other@example.com", "organization_name": "Other Co"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    single = await graphql(
        client,
        "query P($id: UUID!) { prompt(id: $id) { versions { id } } }",
        {"id": theirs},
        other_headers,
    )
    listed = await graphql(client, "{ prompts { id versions { id } } }", headers=other_headers)

    assert single.json()["errors"][0]["extensions"]["code"] == "not_found"
    assert all(p["id"] != theirs for p in listed.json()["data"]["prompts"])


async def test_set_agent_prompt_links_and_unlinks(client, auth_headers):
    agent = await create_agent(client, auth_headers, "Linker")
    agent_id = agent.json()["data"]["createAgent"]["id"]
    prompt_id = await _create_prompt(client, auth_headers, "linked_prompt")
    mutation = (
        "mutation S($a: UUID!, $p: UUID) {"
        " setAgentPrompt(agentId: $a, promptId: $p) { id promptId } }"
    )

    linked = await graphql(client, mutation, {"a": agent_id, "p": prompt_id}, auth_headers)
    assert linked.json()["data"]["setAgentPrompt"]["promptId"] == prompt_id

    agents = await graphql(
        client,
        "query P($id: UUID!) { prompt(id: $id) { agents { id name } } }",
        {"id": prompt_id},
        auth_headers,
    )
    assert agents.json()["data"]["prompt"]["agents"] == [{"id": agent_id, "name": "Linker"}]

    unlinked = await graphql(client, mutation, {"a": agent_id, "p": None}, auth_headers)
    assert unlinked.json()["data"]["setAgentPrompt"]["promptId"] is None


async def test_set_agent_prompt_with_an_unknown_prompt_is_not_found(client, auth_headers):
    agent = await create_agent(client, auth_headers, "Unknown Linker")
    agent_id = agent.json()["data"]["createAgent"]["id"]
    response = await graphql(
        client,
        "mutation S($a: UUID!, $p: UUID) { setAgentPrompt(agentId: $a, promptId: $p) { id } }",
        {"a": agent_id, "p": "00000000-0000-7000-8000-000000000000"},
        auth_headers,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_prompts_agents_is_empty_for_an_unused_prompt(client, auth_headers):
    await _create_prompt(client, auth_headers, "unused_prompt")
    response = await graphql(client, "{ prompts { key agents { id } } }", headers=auth_headers)
    rows = {p["key"]: p["agents"] for p in response.json()["data"]["prompts"]}
    assert rows["unused_prompt"] == []


async def test_default_system_prompt_returns_the_unrendered_default(client, auth_headers):
    response = await graphql(client, "{ defaultSystemPrompt }", headers=auth_headers)
    text = response.json()["data"]["defaultSystemPrompt"]
    assert "{{company_name}}" in text
    assert "{{agent_name}}" in text


async def test_default_system_prompt_requires_authentication(client):
    response = await graphql(client, "{ defaultSystemPrompt }")
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"
