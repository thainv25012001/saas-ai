"""The Phase 1 security gate.

Two real accounts in two organizations, driven only through the public API.
Nothing here reaches into the database directly — the point is to prove that
isolation holds through the same surface a customer would use.
"""

import pytest

pytestmark = pytest.mark.anyio

ORG_A = {
    "email": "a-owner@example.com",
    "password": "correct-horse-battery",
    "full_name": "Owner A",
    "organization_name": "Ada Motors A",
}
ORG_B = {
    "email": "b-owner@example.com",
    "password": "correct-horse-battery",
    "full_name": "Owner B",
    "organization_name": "Ada Motors B",
}


async def _register(client, payload):
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


@pytest.fixture
async def two_accounts(client, clean_users):
    headers_a = await _register(client, ORG_A)
    headers_b = await _register(client, ORG_B)

    created = await _graphql(
        client,
        'mutation { createAgent(input: {name: "Secret A Bot"}) { id } }',
        headers=headers_a,
    )
    agent_a_id = created.json()["data"]["createAgent"]["id"]

    prompt = await _graphql(
        client,
        """
        mutation {
          createPrompt(input: {
            name: "A Prompt", key: "a_secret", systemPrompt: "A's secret prompt"
          }) { id }
        }
        """,
        headers=headers_a,
    )
    prompt_a_id = prompt.json()["data"]["createPrompt"]["id"]
    return headers_a, headers_b, agent_a_id, prompt_a_id


async def test_b_does_not_see_as_agents_in_a_list(two_accounts, client):
    """A positive control alongside the negative assertion: if `list_agents`
    ever regressed to returning an empty list unconditionally, the assertion
    on B alone would stay green. Also querying as A in the same setup state
    proves the list endpoint does return data when there is data to return."""
    headers_a, headers_b, _agent, _prompt = two_accounts
    response_b = await _graphql(client, "{ agents { name } }", headers=headers_b)
    assert response_b.json()["data"]["agents"] == []

    response_a = await _graphql(client, "{ agents { name } }", headers=headers_a)
    assert [a["name"] for a in response_a.json()["data"]["agents"]] == ["Secret A Bot"]


async def test_b_cannot_read_as_agent_by_id(two_accounts, client):
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name } }",
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_update_as_agent(two_accounts, client):
    """A denial that still wrote first and raised second would pass on the
    error code alone, so also confirm as A that the name never changed."""
    headers_a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        'mutation U($id: UUID!) { updateAgent(id: $id, input: {name: "Hijacked"}) { name } }',
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"

    check = await _graphql(
        client, "query A($id: UUID!) { agent(id: $id) { name } }", {"id": agent_a_id}, headers_a
    )
    assert check.json()["data"]["agent"]["name"] == "Secret A Bot"


async def test_b_cannot_delete_as_agent(two_accounts, client):
    """As above: confirm as A that the agent still exists after B's denied
    delete, not just that B's own call returned `not_found`."""
    headers_a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "mutation D($id: UUID!) { deleteAgent(id: $id) }",
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"

    check = await _graphql(
        client, "query A($id: UUID!) { agent(id: $id) { name } }", {"id": agent_a_id}, headers_a
    )
    assert check.json()["data"]["agent"]["name"] == "Secret A Bot"


async def test_b_cannot_read_as_agent_config(two_accounts, client):
    """As above: confirm as A that the config's `tone` is still the
    server-side default ("friendly"), not the "rude" value B's denied
    mutation tried to write."""
    headers_a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        """
        mutation U($id: UUID!) {
          updateAgentConfig(agentId: $id, input: {tone: "rude"}) { tone }
        }
        """,
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"

    check = await _graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { config { tone } } }",
        {"id": agent_a_id},
        headers_a,
    )
    assert check.json()["data"]["agent"]["config"]["tone"] == "friendly"


async def test_b_gets_no_data_when_nesting_config_under_as_agent(two_accounts, client):
    """This does NOT exercise the `config` dataloader's own tenant check —
    `agent(id)` raises `not_found` inside `Query.agent` before the nested
    `config` field ever resolves, so the dataloader never runs here. What
    this proves is narrower but still worth having: the outer lookup fails
    closed with a clean `not_found` and no partial `data` payload, for a
    query shape (`agent(id) { config { ... } }`) distinct from the
    `updateAgentConfig` mutation above. The dataloader itself
    (`Context._load_configs` in app/graphql/context.py) carries both layers
    independently — an `organization_id` predicate of its own plus Postgres
    RLS on `agent_configs` — and each of those is exercised on its own in
    tests/integration/test_isolation_layers.py."""
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name config { tone } } }",
        {"id": agent_a_id},
        headers_b,
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"
    assert body["data"] is None or body["data"].get("agent") is None


async def test_b_does_not_see_as_prompts(two_accounts, client):
    """Positive control for the same reason as the agents-list test above:
    prove A's prompt is actually returned by this same query, not just that
    B's query happens to come back empty."""
    headers_a, headers_b, _agent, _prompt = two_accounts
    response_b = await _graphql(client, "{ prompts { key } }", headers=headers_b)
    assert response_b.json()["data"]["prompts"] == []

    response_a = await _graphql(client, "{ prompts { key } }", headers=headers_a)
    assert [p["key"] for p in response_a.json()["data"]["prompts"]] == ["a_secret"]


async def test_b_cannot_read_as_prompt_by_id(two_accounts, client):
    _a, headers_b, _agent, prompt_a_id = two_accounts
    response = await _graphql(
        client,
        "query P($id: UUID!) { prompt(id: $id) { key } }",
        {"id": prompt_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_add_a_version_to_as_prompt(two_accounts, client):
    _a, headers_b, _agent, prompt_a_id = two_accounts
    response = await _graphql(
        client,
        """
        mutation V($id: UUID!) {
          createPromptVersion(promptId: $id, input: {systemPrompt: "injected"}) { id }
        }
        """,
        {"id": prompt_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_activate_a_version_of_as_prompt(two_accounts, client, owner_connection):
    """`activatePromptVersion` is `createPromptVersion`'s sibling mutation and
    the one that actually changes which prompt version an agent serves. B
    must not be able to activate a version that belongs to A's prompt, even
    though B never learns A's version id through any legitimate query — the
    id here comes from A's own setup, standing in for a leaked or guessed
    id.

    A denial that wrote first and raised second — plausible here, since
    `activate_version` both deactivates the previously-active version and
    activates the target one — would pass on the error code alone. This
    schema has no query exposing a PromptVersion's `isActive` by id (`Prompt`
    does not expose its versions at all), so there is no way to check that
    "v1 is still active and v2 still is not" through the public API. The
    attack itself is still driven entirely through GraphQL; only this one
    read-only postcondition check falls back to `owner_connection`, because
    the front door has no way to ask the question."""
    headers_a, headers_b, _agent, prompt_a_id = two_accounts
    version = await _graphql(
        client,
        """
        mutation V($id: UUID!) {
          createPromptVersion(promptId: $id, input: {systemPrompt: "v2 for A"}) { id }
        }
        """,
        {"id": prompt_a_id},
        headers_a,
    )
    version_a_id = version.json()["data"]["createPromptVersion"]["id"]

    response = await _graphql(
        client,
        "mutation Act($id: UUID!) { activatePromptVersion(versionId: $id) { id isActive } }",
        {"id": version_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"

    from sqlalchemy import text

    rows = await owner_connection.execute(
        text("SELECT id, is_active FROM prompt_versions WHERE prompt_id = :prompt_id"),
        {"prompt_id": prompt_a_id},
    )
    active_by_id = {str(row.id): row.is_active for row in rows}
    assert active_by_id[version_a_id] is False
    # v1 and v2 are the only two versions of this prompt; exactly one
    # (v1) is still active, proving B's denied call deactivated nothing.
    assert sum(1 for is_active in active_by_id.values() if is_active) == 1


async def test_me_reports_each_owners_own_organization(two_accounts, client):
    headers_a, headers_b, _agent, _prompt = two_accounts
    a = await _graphql(client, "{ me { organizationName } }", headers=headers_a)
    b = await _graphql(client, "{ me { organizationName } }", headers=headers_b)
    assert a.json()["data"]["me"]["organizationName"] == "Ada Motors A"
    assert b.json()["data"]["me"]["organizationName"] == "Ada Motors B"


async def test_organization_query_reports_each_owners_own_organization(two_accounts, client):
    """The `organization` query takes no id, so the only thing deciding which
    row comes back is the caller's own token. Two accounts must therefore see
    two different organizations."""
    headers_a, headers_b, _agent, _prompt = two_accounts
    query = "{ organization { id name slug plan } }"
    a = (await _graphql(client, query, headers=headers_a)).json()["data"]["organization"]
    b = (await _graphql(client, query, headers=headers_b)).json()["data"]["organization"]

    assert a["name"] == "Ada Motors A"
    assert b["name"] == "Ada Motors B"
    assert a["id"] != b["id"]
    assert a["slug"] != b["slug"]


async def test_a_can_still_see_its_own_agent(two_accounts, client):
    """The mirror of every test above: isolation that also blocks the owner
    is a bug, not security."""
    headers_a, _b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name } }",
        {"id": agent_a_id},
        headers_a,
    )
    assert response.json()["data"]["agent"]["name"] == "Secret A Bot"
