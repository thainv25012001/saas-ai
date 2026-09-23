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


def hash_text(text: str) -> str:
    """SHA-256 hex digest of an already-computed `embeddable_text` string.

    Split out from `hash_embeddable_text` (below) so a caller that already
    has the exact string it just embedded -- `app/products/embedding.py`'s
    `embed_and_store`, most pointedly -- can hash *that string*, not
    re-derive one from the row's current `name`/`description`/`attributes`.
    Re-deriving is not equivalent: those attributes live on a mutable ORM
    object, and if anything changes them between "compute the text to
    embed" and "compute the hash to store" (an `await` sits in between),
    the stored hash would describe different text than the vector actually
    came from -- exactly the divergence `embedding_source_hash` exists to
    make impossible to have and not know about. Hashing the captured
    string makes that structurally impossible rather than merely unlikely.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_embeddable_text(name: str, description: str | None, attributes: dict[str, Any]) -> str:
    """SHA-256 hex digest of `embeddable_text`, stored as
    `Product.embedding_source_hash` -- a fingerprint of the text the row's
    *currently stored* embedding was computed from, independent of whether
    that embedding is fresh or stale.

    A thin composition of `embeddable_text` and `hash_text`: kept as its
    own function (rather than inlined at every call site) because most
    callers -- `ProductService.create`/`upsert_many` -- have the row's
    fields in hand and want the hash directly, with no intermediate string
    to keep around.
    """
    return hash_text(embeddable_text(name, description, attributes))
