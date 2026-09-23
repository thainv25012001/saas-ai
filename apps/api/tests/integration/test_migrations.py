import pytest
from sqlalchemy import text

pytestmark = pytest.mark.anyio


async def test_identity_tables_exist(owner_connection):
    result = await owner_connection.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    )
    tables = {row[0] for row in result}
    assert {"organizations", "users", "memberships"} <= tables


async def test_email_column_is_citext(owner_connection):
    """citext is what makes the unique index case-insensitive."""
    result = await owner_connection.execute(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'email'"
        )
    )
    assert result.scalar_one() == "citext"


async def test_membership_is_unique_per_org_and_user(owner_connection):
    result = await owner_connection.execute(
        text(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE tablename = 'memberships' "
            "AND indexdef LIKE '%UNIQUE%organization_id, user_id%'"
        )
    )
    assert result.scalar_one() == 1


@pytest.mark.parametrize("table", ["organizations", "users", "memberships"])
async def test_identity_tables_do_not_have_rls(owner_connection, table):
    """organizations/users/memberships are reached through membership joins,
    not through a tenant setting, so they are deliberately excluded from RLS."""
    result = await owner_connection.execute(
        text(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE relname = :table AND relnamespace = 'public'::regnamespace"
        ),
        {"table": table},
    )
    assert result.scalar_one() is False


@pytest.mark.parametrize(
    "table",
    [
        "agents",
        "agent_configs",
        "prompts",
        "prompt_versions",
        "conversations",
        "messages",
        "usage_events",
        "documents",
        "document_chunks",
        "message_citations",
        "tools",
        "agent_tools",
        "message_tool_calls",
        "leads",
        "products",
        "eval_datasets",
        "eval_cases",
        "eval_runs",
        "eval_results",
    ],
)
async def test_tenant_tables_have_rls_enabled_with_a_tenant_isolation_policy(
    owner_connection, table
):
    """RLS being both *enabled* and carrying the `tenant_isolation` policy is
    itself a security invariant these business tables must hold, not just an
    implementation detail: if a future migration silently dropped the policy
    from one of these tables, every isolation test that routes through a
    service-layer ownership check first would stay green while that table
    quietly lost a whole layer of defence.

    This test only asserts the policy *exists* — it says nothing about what
    the predicate does, so a policy weakened to `USING (true)` would still
    pass here. What the predicate actually enforces, with no service filter
    in the picture, is asserted in tests/integration/test_isolation_layers.py
    ::test_rls_alone_hides_another_orgs_agents_from_raw_sql.

    `documents`/`document_chunks` (Task 2) join the original agents/prompts
    list here rather than getting their own parametrized block --
    document_chunks in particular is the table whose FK checks bypass RLS
    entirely (see DocumentService.replace_chunks), so RLS being enabled here
    is one layer of defence among several, not the only one, but it must
    still be present."""
    enabled = await owner_connection.execute(
        text(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE relname = :table AND relnamespace = 'public'::regnamespace"
        ),
        {"table": table},
    )
    assert enabled.scalar_one() is True

    policy_count = await owner_connection.execute(
        text(
            "SELECT COUNT(*) FROM pg_policy "
            "JOIN pg_class ON pg_class.oid = pg_policy.polrelid "
            "WHERE pg_class.relname = :table AND pg_policy.polname = 'tenant_isolation'"
        ),
        {"table": table},
    )
    assert policy_count.scalar_one() == 1


async def test_readiness_reports_dependencies_up(client):
    response = await client.get("/health/ready")
    assert response.json()["checks"] == {"database": True, "redis": True}


@pytest.mark.parametrize(
    ("index_name", "indexdef_fragment"),
    [
        # HNSW on embedding: Task 4's semantic arm.
        ("ix_products_embedding", "USING hnsw (embedding vector_cosine_ops)"),
        # GIN on search_tsv: Task 4's keyword arm.
        ("ix_products_search_tsv", "USING gin (search_tsv)"),
        # GIN on attributes: Task 4's jsonb filter arm depends on this one
        # existing, not just on the column existing.
        ("ix_products_attributes", "USING gin (attributes)"),
        # Composite btree: (organization_id, category) equality plus a
        # trailing price range, see 0010_products.py for why one index
        # rather than two.
        (
            "ix_products_organization_id_category_price",
            "(organization_id, category, price)",
        ),
    ],
)
async def test_products_has_the_indexes_task_4_depends_on(
    owner_connection, index_name, indexdef_fragment
):
    result = await owner_connection.execute(
        text("SELECT indexdef FROM pg_indexes WHERE tablename = 'products' AND indexname = :name"),
        {"name": index_name},
    )
    indexdef = result.scalar_one_or_none()
    assert indexdef is not None, f"missing index {index_name}"
    assert indexdef_fragment in indexdef


async def test_products_unique_constraint_covers_organization_id_and_external_id(
    owner_connection,
):
    """This is the mechanism that makes Task 3's re-import an upsert rather
    than a duplicate -- see ProductService.upsert_many."""
    result = await owner_connection.execute(
        text(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE tablename = 'products' "
            "AND indexdef LIKE '%UNIQUE%organization_id, external_id%'"
        )
    )
    assert result.scalar_one() == 1


async def test_products_search_tsv_generation_expression_excludes_volatile_columns(
    owner_connection,
):
    """Pins docs/PHASE-5.md §4 at the schema level: whatever the service
    layer does, the generated expression itself must never reference
    price/stock_quantity/availability, or a price change could never be a
    plain UPDATE."""
    result = await owner_connection.execute(
        text(
            "SELECT generation_expression FROM information_schema.columns "
            "WHERE table_name = 'products' AND column_name = 'search_tsv'"
        )
    )
    expression = result.scalar_one()
    assert "'english'" in expression
    assert "name" in expression
    assert "description" in expression
    assert "category" in expression
    assert "price" not in expression
    assert "stock_quantity" not in expression
    assert "availability" not in expression


async def test_products_has_embedding_staleness_columns(owner_connection):
    """`embedding_source_hash`/`embedding_stale` are what make an
    embedding-less upsert's stale vector observable instead of a silent
    assumption -- see ProductService.upsert_many. Pinned at the schema
    level because a migration edit that dropped or mistyped either would
    otherwise only surface as a mypy/runtime error in the service, not a
    reddened test naming the actual missing guarantee."""
    result = await owner_connection.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'products' "
            "AND column_name IN ('embedding_source_hash', 'embedding_stale')"
        )
    )
    columns = {row.column_name: row for row in result}
    assert columns["embedding_source_hash"].is_nullable == "YES"
    assert columns["embedding_stale"].is_nullable == "NO"
    assert columns["embedding_stale"].data_type == "boolean"
    assert columns["embedding_stale"].column_default == "false"


@pytest.mark.parametrize(
    ("table", "index_name", "indexdef_fragment"),
    [
        # `EvaluationService.list_cases`/`count_cases`'s own access pattern.
        (
            "eval_cases",
            "ix_eval_cases_organization_id_dataset_id",
            "(organization_id, dataset_id)",
        ),
        # `list_runs`'s "newest first" ordering, per dataset.
        (
            "eval_runs",
            "ix_eval_runs_organization_id_dataset_id_created_at",
            "(organization_id, dataset_id, created_at DESC)",
        ),
        # Partial: only a pending/running row can block a new run of the same
        # dataset (docs/PHASE-6.md §5) -- Task 4's one-active-run check.
        ("eval_runs", "ix_eval_runs_active_by_dataset", "WHERE (status = ANY"),
    ],
)
async def test_evaluations_has_the_indexes_task_4_depends_on(
    owner_connection, table, index_name, indexdef_fragment
):
    result = await owner_connection.execute(
        text("SELECT indexdef FROM pg_indexes WHERE tablename = :table AND indexname = :name"),
        {"table": table, "name": index_name},
    )
    indexdef = result.scalar_one_or_none()
    assert indexdef is not None, f"missing index {index_name}"
    assert indexdef_fragment in indexdef


async def test_eval_results_unique_constraint_covers_run_and_case(owner_connection):
    """What makes Task 4's `ON CONFLICT (run_id, case_id) DO NOTHING` a real
    upsert target instead of an error -- see docs/PHASE-6.md §3."""
    result = await owner_connection.execute(
        text(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE tablename = 'eval_results' AND indexdef LIKE '%UNIQUE%run_id, case_id%'"
        )
    )
    assert result.scalar_one() == 1
