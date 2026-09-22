"""The text a product's embedding is computed from, and a hash of it.

`docs/ARCHITECTURE.md` §3.4: "embedding vector(1536) -- over name +
description + key attributes". Deliberately not `category` (`search_tsv`'s
third input -- see `alembic/versions/0010_products.py`) and not
`price`/`stock_quantity`/`availability` (`docs/PHASE-5.md` §4): this table
already keeps every volatile field in its own dedicated column, so anything
landing in `attributes` is stable by construction and safe to embed.

`embeddable_text` is the single source of truth both Task 2's real embedding
generation and `hash_embeddable_text` (below) build from, so the embedding
and the hash that claims to describe it can never drift apart by being
computed from two different serialisations of the same row -- see
`ProductService.upsert_many` for what the hash is for and why it exists.
"""

import hashlib
import json
from typing import Any


def embeddable_text(name: str, description: str | None, attributes: dict[str, Any]) -> str:
    """`attributes` is serialised with sorted keys: it is a jsonb column, its
    key order carries no meaning, and Python's own dict order is not
    guaranteed to match whatever order a caller (or an upsert re-read from
    the database) happens to produce it in. Hashing an arbitrary order would
    make `hash_embeddable_text` report "content changed" for a row whose
    content did not."""
    return "\n".join([name, description or "", json.dumps(attributes, sort_keys=True, default=str)])


def hash_embeddable_text(name: str, description: str | None, attributes: dict[str, Any]) -> str:
    """SHA-256 hex digest of `embeddable_text`, stored as
    `Product.embedding_source_hash` -- a fingerprint of the text the row's
    *currently stored* embedding was computed from, independent of whether
    that embedding is fresh or stale."""
    return hashlib.sha256(
        embeddable_text(name, description, attributes).encode("utf-8")
    ).hexdigest()
