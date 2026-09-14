import pytest

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
