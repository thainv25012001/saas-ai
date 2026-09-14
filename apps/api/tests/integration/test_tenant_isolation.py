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
    _a, headers_b, _agent, _prompt = two_accounts
    response = await _graphql(client, "{ agents { name } }", headers=headers_b)
    assert response.json()["data"]["agents"] == []


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
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        'mutation U($id: UUID!) { updateAgent(id: $id, input: {name: "Hijacked"}) { name } }',
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_delete_as_agent(two_accounts, client):
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "mutation D($id: UUID!) { deleteAgent(id: $id) }",
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_read_as_agent_config(two_accounts, client):
    _a, headers_b, agent_a_id, _prompt = two_accounts
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


async def test_b_cannot_read_as_agent_config_via_nested_field(two_accounts, client):
    """`agent(id) { config { ... } }` resolves `config` through a dataloader
    rather than through AgentService — a different code path from
    `updateAgentConfig` above. The outer `agent(id)` lookup is expected to
    fail closed before the nested field ever runs, so this must still come
    back as a clean `not_found` rather than leaking a partial `data` payload
    with `agent: null` and a config value alongside it."""
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
    _a, headers_b, _agent, _prompt = two_accounts
    response = await _graphql(client, "{ prompts { key } }", headers=headers_b)
    assert response.json()["data"]["prompts"] == []


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


async def test_b_cannot_activate_a_version_of_as_prompt(two_accounts, client):
    """`activatePromptVersion` is `createPromptVersion`'s sibling mutation and
    the one that actually changes which prompt version an agent serves. B
    must not be able to activate a version that belongs to A's prompt, even
    though B never learns A's version id through any legitimate query — the
    id here comes from A's own setup, standing in for a leaked or guessed
    id."""
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


async def test_me_reports_each_owners_own_organization(two_accounts, client):
    headers_a, headers_b, _agent, _prompt = two_accounts
    a = await _graphql(client, "{ me { organizationName } }", headers=headers_a)
    b = await _graphql(client, "{ me { organizationName } }", headers=headers_b)
    assert a.json()["data"]["me"]["organizationName"] == "Ada Motors A"
    assert b.json()["data"]["me"]["organizationName"] == "Ada Motors B"


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
