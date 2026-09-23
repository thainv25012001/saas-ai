"""Rendering a Python embedding as a pgvector SQL literal -- shared by
`app/rag/retrieve.py` and `app/rag/products.py`, the only two places in
this codebase that execute raw SQL against a `vector` column via
`CAST(:param AS vector)` rather than the ORM. Both need it because no
pgvector codec is registered on this session's asyncpg connections, so a
query vector cannot travel as a driver-level typed parameter -- it has to
be rendered as a string and cast in SQL, the same pattern the ingestion
tests already use for seeding vector columns via raw SQL.

Lives in `app/rag`, not `app/core` (where `app/core/rrf.py`'s Reciprocal
Rank Fusion sits): RRF is a pure algorithm with no infrastructure coupling
at all -- it knows nothing about SQL, pgvector, or asyncpg. This is
specifically a raw-SQL/pgvector concern that matters only to the two
retrieval modules issuing that SQL; nothing outside `app/rag` needs it
(every other write path uses the ORM's `Vector` type, which handles this
transparently), so `app/rag` is where the concern actually lives.

Second instance of the exact pattern Ruling 3 (Task 4) exists to prevent
-- found during that task's own review, in the same commit that first
discharged Ruling 3 for the RRF fusion -- so it is closed the same way:
extracted here, both call sites converted in the same commit, not left as
two byte-identical private copies for a later cleanup to find.
"""


def vector_literal(values: list[float]) -> str:
    """Render an embedding as a pgvector text-input literal, e.g. "[0.1,-0.2]"."""
    return "[" + ",".join(repr(value) for value in values) + "]"
