"""`search_products`/`get_product`: Task 5's pair, and the reason Phase 5 is
reachable at all (`docs/PHASE-5.md` §2). Without these, "what does the
Camry LE cost" can only ever be answered from a retrieved document chunk --
prose that happens to mention a price -- because there is no other source
in the system. This module does not make that violation impossible (see
this module's own note on the no-floor decision below, and §2 for the
larger argument that Phase 5 does not fully close the gap either); it makes
the tool answer the cheaper, more attractive path, per §2 item 1: structured
fields the model does not have to parse out of prose, and a `source`
naming the exact row on every result.

Follows `app/tools/retrieve.py`'s shape closely -- clamped result count, an
explicit not-found/no-results message, a savepoint around the database
work, `ToolContext` supplying tenancy rather than any model-supplied
argument -- because both are read-only builtins answering the identical
question ("what did this tool actually find, and how do I tell the model
tenant-safely").

**The no-relevance-floor consequence, and what this module does about it
(task-5-brief.md, "The design decision I want argued, not defaulted").**
`ProductSearchService.search` (Task 4) deliberately ships with no default
relevance floor: `settings.product_search_max_cosine_distance`/
`product_search_min_keyword_rank` default to `None`, because every
offline measurement available to calibrate one comes from `HashingEmbedder`
-- a test double whose distance distribution has no principled relationship
to a production embedding model's. A calibrated default would be a
number wearing a measurement's clothes, and Task 4's own docstring
documents the measured consequence directly: an unrelated query against a
ten-row car catalogue still returns all ten rows, at cosine distance
0.9139-1.0000, while a genuine match on the same corpus scores 0.1294.

Three responses were on the table:

1. **Pick a fixed cutoff anyway** (e.g. hide any result with
   `vector_distance` above some constant). Rejected for exactly the reason
   Task 4 gave for not doing this one layer down: no number available today
   is a measurement of what a real embedder's distances mean, so any
   constant chosen now is a guess that would have to be re-guessed the
   moment a real provider is configured -- and a wrong guess fails *silently*
   in the specific way `docs/PHASE-5.md` §2 warns about: a genuinely
   relevant product just above the line vanishes with no signal to anyone
   that it happened.
2. **Do nothing and leave it to Phase 6.** Phase 6 is evaluation, and
   evaluation is exactly the machinery that could eventually calibrate a
   real threshold against a real embedder and real labelled queries -- but
   "leave it" here means a customer asking a car dealership about scuba
   gear gets three cars back, structured and confident, and the model has
   no reason not to recommend one. That is not a smaller version of the
   problem this task exists to reduce; it is the problem, reachable on the
   very first query Phase 5 answers.
3. **Surface the raw signal honestly instead of filtering on it** -- the
   option this module implements. `ProductMatch.vector_distance`/
   `.keyword_rank` already exist on the contract *specifically* so a caller
   can make this decision (Task 4's own docstring says so); the choice made
   here is to hand them to the one party who does not need a calibrated
   number to use them usefully: the model itself, on this one call, in the
   context of this one question. Each result's `data` payload carries the
   literal numbers, per match, for the model to weigh, and `content` (only
   when a `query` actually drove ranking -- filters-only browsing has no
   relevance question to caveat) is prefixed with a note about what the
   returned set looks like. This reports match quality instead of hiding
   it, without smuggling a magic number in as if it had been measured. It
   does not stop a model that ignores the note from recommending anyway --
   no prompt rule fully holds, per §2 item 2 -- but that is the same
   partial-mitigation shape every other layer of §2 already accepts, not a
   new gap this module introduces.

   **The note is keyed on the SHAPE of the returned set, not a constant
   string** (`_match_shape_note`, added in this task's fix round). The
   first version of this module used one fixed sentence regardless of
   whether the top result was the measured-excellent case (cosine distance
   0.1294) or the measured-garbage case (0.9139-1.0000, all ten rows) --
   which has two problems, and the second is the one that matters. It risks
   exactly the failure this brief warns about: hedging on a genuinely good
   match is a new harm, not a fix for the old one. And more fundamentally,
   **a note that always fires carries no information** -- `docs/PHASE-5.md`
   §2 item 2 already says prompt rules alone do not hold here, and a
   constant disclaimer is the weakest possible form of one, since the model
   has no way to tell "be careful here" apart from "no need to be", so it
   degrades into noise a model learns to skip.

   The fix keys the note on a discriminator that needs no cross-embedder
   calibration -- the ratio between a leading group's own trailing gap and
   the total spread of the returned set's OWN `vector_distance` values
   (`_SHARP_LEADER_GAP_RATIO`, `_classify_distance_shape` below). This is
   self-referential: it is computed from, and only ever compared against,
   the SAME query's own returned distances, so -- unlike an absolute cosine
   -distance cutoff -- it carries no assumption about what any one
   embedder's numbers mean, which is the exact constraint that ruled out
   option 1 above. A sharp leader (one or more results meaningfully closer
   than everything else) gets a note that says so, without claiming any of
   them is a GOOD match in any absolute sense -- only that they stand out
   from what follows. A flat verdict (no admissible boundary had a big
   enough gap) gets a note stating only that narrower fact -- as of fix
   round 4, deliberately NOT "nothing stands out" (see `_FLAT_BAND_NOTE`'s
   own comment for why that stronger claim turned out to be false in a
   shape this same classifier is asked to handle). Degrades safely to the
   original, uniform note whenever the shape cannot be assessed honestly:
   too few results with a real `vector_distance` to have a leader and a
   "rest" to compare it against at all (`_MIN_SHAPE_SAMPLE`) -- which is
   every single-result set, and every result set the keyword arm alone
   produced. Never a confident claim the data does not support; the
   strong claim is the one that can be wrong.

   **Fix round 2: a GROUP, not just an item.** The first version of this
   heuristic compared only the best result to the second-best. A reviewer
   built the realistic shape `docs/PHASE-5.md` §3 itself names as one of
   three query types this subsystem exists to serve -- "Camry LE vs Camry
   SE", two genuinely close matches sitting above a real noise tail
   (`[0.13, 0.15, 0.70, 0.75, 0.79, 0.85, 0.91, 0.92, 0.93, 1.00]`) -- and
   that version called it "flat", then told the model nothing stood out
   about a set where two results plainly did. That is not merely
   uninformative, the failure mode fix round 1 was about; it is a note
   asserting something FALSE, which is worse, for the same reason a hash
   that certifies the wrong text is worse than no hash -- it reads as a
   finding. `_classify_distance_shape` now checks every candidate leading
   -group boundary up to half the set (a structural bound derived from the
   set's own size -- a "leading group" cannot be the majority of the list
   and still be leading a tail -- not a second tuned number) and keeps
   whichever boundary has the largest gap relative to the set's own total
   spread. A single clear leader is still just this same loop's
   `group_size == 1` case.

   **Fix round 3: the same bug, one size down.** The "cannot be the
   majority of the set" cap from fix round 2 is itself expressed as a
   fraction of the WHOLE set (`len // 2`), and that turned out to be
   wrong at small `n` for the identical reason a fixed cosine-distance
   cutoff was wrong in the first place -- it does not scale. At `n = 10`
   the cap correctly excludes a group of nine (leaving one real outlier
   behind is not "leading a tail"). At `n = 3`, the SAME cap excludes a
   group of two (`[0.10, 0.12, 0.90]`, `n // 2 = 1`) -- and a
   `search_products` call returning three results from a small `limit` or
   a filtered catalogue is entirely ordinary, not a corner case. What
   actually needs to hold is not "the group is a minority of everything"
   but "the group does not outnumber its own REMAINDER too heavily" --
   `_classify_distance_shape` now admits a candidate boundary only when
   `boundary * _LEADING_GROUP_REMAINDER_RATIO <= remainder` (the group is
   at most twice what it leaves behind). One rule, not a small-`n` special
   case alongside a large-`n` one.

   **Fix round 4: stop generalising the boundary, and weaken the FLAT
   note instead.** A third sweep (`n = 2..12`) found the identical class
   of false statement again -- at `n=7`, five good results near 0.10 and
   two bad ones near 0.80 (a leading group of five, remainder two) fell
   outside `_LEADING_GROUP_REMAINDER_RATIO`'s admission rule
   (`5 * 0.5 = 2.5 > 2`), so `_FLAT_BAND_NOTE` fired and (in its fix
   -round-3 wording) claimed nothing stood out, while five results
   plainly did. Three rounds relocating the same cutoff -- `n // 2`, then
   a remainder ratio -- is the pattern that says the cutoff itself is the
   wrong kind of fix: "how large can a leading group be before it stops
   leading" is a judgement call with no distance to recover it from, so
   every count-based admission rule will have an edge, and every wider
   sweep will find it. The exit is to stop asserting a claim strong
   enough to need one: `_FLAT_BAND_NOTE` no longer says nothing stands
   out. It says only what this branch actually establishes -- that no
   SINGLE result clearly separates from the rest -- which stays true
   whether the branch was reached because the set is genuinely
   undifferentiated (Task 4's measured unrelated-query band, a smooth
   gradient) or because a GROUP of several separated cleanly while no
   individual one did. The SHARP path is untouched: every round's error
   in that direction has been an under-claim (calling a real leading
   group flat), never an over-claim, which is the safe direction to leave
   alone. `_SHARP_LEADER_GAP_RATIO` (a distance ratio: how big a gap must
   be) and `_LEADING_GROUP_REMAINDER_RATIO` (a count ratio: how large a
   remainder a candidate group must leave) are now two separately named
   constants rather than one reused number -- fix round 3's "these are
   the same question in different units" reasoning was a coupling between
   two conceptually distinct quantities that happened to share a value,
   not a derived equivalence between them, and retuning one for its own
   reasons would have silently moved the other.

**Citations.** `docs/ARCHITECTURE.md` §5.4 rule 3 ("message_citations
records what was actually retrieved") is implemented for products the
identical way it already is for chunks: every product that reaches the
prompt -- from either tool -- gets a `CitationPayload` with `product_id`
set (`chunk_id`/`document_id` left `None`, the widened shape
`app/rag/retrieve.py` now carries), which `ChatService._record_citations`
persists onto `message_citations.product_id`. `get_product` emits one, not
zero, for the same reason `search_products` emits one per result: a fetched
product's data reaches the prompt exactly as surely as a searched one does,
and Phase 6's whole premise (§2 item 3) is that this must be answerable
after the fact, not merely true of the tool the brief happened to name
first.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.tenancy import TenantContext
from app.db.models import Product, ProductAvailability
from app.embeddings.base import EmbeddingProvider
from app.products.service import ProductService
from app.rag.products import ProductMatch, ProductSearchService
from app.rag.retrieve import CitationPayload
from app.tools.base import AgentTool, ToolContext, ToolResult

# Same reasoning as `app/tools/retrieve.py`'s `_MAX_TOP_K`: `limit` reaches
# this tool as a value a model chose, not one a trusted caller passed, so it
# is clamped to what the tool will ever hand back rather than validated and
# rejected. Twenty, not ten (retrieval's own bound): a product listing
# ("everything under £30,000") is a legitimate browse-the-catalogue request
# in a way a document search rarely is, and a product's structured entry is
# far smaller than an assembled document passage, so a wider cap costs
# proportionally less prompt budget.
_MAX_LIMIT = 20

_NO_RESULTS_MESSAGE = (
    "No products matched this search. Do not invent a product, price, or "
    "availability; tell the user nothing in the catalogue matches."
)

_NOT_FOUND_MESSAGE = "No product found with that id."

# Added to `content` only for a ranked (query-driven) search -- a
# filters-only browse has no relevance question to caveat, since every
# surviving row satisfied an exact predicate rather than a similarity
# score. Which of the three notes below fires is decided by
# `_match_shape_note`; see this module's own docstring ("The note is keyed
# on the SHAPE...") for why a single constant string was replaced with this.

# Degrade-safe fallback: whenever the shape of the returned set cannot be
# assessed honestly (`_MIN_SHAPE_SAMPLE`), this is the ONLY note that fires
# -- unchanged from the fix round's original, uniform note, since "I cannot
# tell" is still an honest thing to say and was never the problem with it.
_AMBIGUOUS_MATCH_QUALITY_NOTE = (
    "Results are ranked by relevance to your query but are NOT filtered by "
    "a minimum relevance threshold, and there are too few results here to "
    "tell whether the top one stands out or is just the least-bad option. "
    "Check each result's vector_distance (lower = closer semantic match; "
    "absent means the product has no embedding yet) and keyword_rank "
    "(absent means no exact lexical overlap with your query) before "
    "presenting a result as relevant. Do not assume the first result "
    "answers the question."
)

# A sharp leader: one or more of the top results' vector_distance are
# clearly separated from the rest of the returned set (see
# `_match_shape_note`) -- a LEADING GROUP, not necessarily a single item:
# "Camry LE vs Camry SE" (docs/PHASE-5.md §3's own named case) is two
# genuinely close matches sitting above a noise tail, and a note that can
# only ever recognise a single leader would call that shape "flat" and be
# WRONG, not merely uninformative. Deliberately does NOT say any result is
# a good match -- only that it (or they) stand out from what follows,
# which is the strongest claim this signal supports.
_SHARP_LEADER_NOTE = (
    "The top result(s) in this list have a vector_distance clearly "
    "separated from the rest -- a real gap, not noise -- which is some "
    "evidence they are more relevant than what follows. That is not the "
    "same as being a good match in an absolute sense: verify whichever "
    "result(s) you rely on actually answer the question, and check their "
    "keyword_rank too, before presenting one as the answer."
)

# NOT "nothing stands out" (fix round 4). Three rounds of review each
# found a shape where a stronger flat note was FALSE: a leading GROUP of
# several results can separate cleanly from a tail while no SINGLE result
# does (`n=7`, five good results near 0.10, two bad near 0.80-- the
# classifier's own boundary search is capped by construction and cannot
# recognise every such group without also wrongly recognising a lone
# straggler as a "group", so somewhere a real group will fall outside
# whatever boundary range is admitted). Chasing that boundary for a fourth
# round would only relocate the same failure again -- "how large can a
# leading group be before it stops leading" has no principled answer to
# converge on; it is a judgment call, not a fact recoverable from the
# distances. So this note now asserts only what the classifier actually
# established when it takes this branch: no ONE result clearly separates
# from the rest -- true in every shape this module has been tested
# against, including the ones where several results plainly do cluster
# near the top, where it is weaker than the classifier's own confidence,
# deliberately. Do NOT strengthen this back into a claim about the whole
# set having nothing worthwhile in it -- that is the exact claim three
# rounds of review found a counterexample to.
_FLAT_BAND_NOTE = (
    "Ranking did not identify a single result whose vector_distance "
    "clearly separates it from the rest of this list. That does not mean "
    "every result is equally weak -- several results could still be "
    "genuinely close matches as a group -- only that no ONE result stands "
    "out on its own. Check each result's vector_distance and keyword_rank "
    "before treating any of them as clearly relevant, and do not assume "
    "the first result is meaningfully better than the others just because "
    "it is listed first."
)

# How many results with a real `vector_distance` are needed before the
# returned set's shape says anything at all. Below this, "sharp leader" and
# "flat band" are not merely unmeasured, they are UNKNOWABLE: one point has
# no gap to speak of, and two points have exactly one gap with nothing of
# its own to compare that gap against. The honest answer below this many
# points is "I cannot tell", never a shape claim this data cannot support.
_MIN_SHAPE_SAMPLE = 3

# The fraction of the returned set's OWN distance range (worst minus best)
# that a leading group's own trailing gap must occupy before the set
# counts as having a genuine leader. A RATIO, not a cosine distance:
# computed from, and only ever compared against, the SAME query's own
# returned distances, so -- unlike a fixed cosine-distance cutoff -- it
# carries no assumption about what any one embedder's numbers mean in
# absolute terms, which is exactly the constraint that ruled out an
# absolute cutoff in the first place (see this module's docstring). 0.5
# says "the gap behind the leading group is at least as large as the
# entire spread of everything else."
#
# Fix round 3 reused this same value for a SECOND purpose -- whether a
# candidate group size was even worth checking (see
# `_LEADING_GROUP_REMAINDER_RATIO` below) -- reasoning that the two were
# "the same question asked in a different unit". Fix round 4 undoes that:
# a distance ratio and a count ratio are genuinely different quantities
# that happened to share a value by choice, not by any equivalence
# between them, and coupling them meant retuning one for its own reasons
# would have silently moved the other. Two named constants, kept at the
# same value because neither round has a reason yet to diverge them, not
# because they are secretly one number.
_SHARP_LEADER_GAP_RATIO = 0.5

# How large a candidate leading group's own REMAINDER must be, relative to
# the group's size, before that boundary is even considered as a
# candidate "leader" (`_classify_distance_shape`'s `boundary *
# _LEADING_GROUP_REMAINDER_RATIO <= remainder`) -- a count ratio, not a
# distance ratio (see `_SHARP_LEADER_GAP_RATIO`'s own comment for why
# these are two constants, not one, as of fix round 4). Self-referential
# in the identical way: computed from, and only ever compared against, the
# SAME returned set's own size, so it carries no assumption about how
# large a "normal" result set is. 0.5 says "the group is at most twice
# its own remainder" -- a structural bound on the shape of the candidate,
# independent of whatever gap-strength threshold `_SHARP_LEADER_GAP_RATIO`
# separately applies once a candidate is admitted.
_LEADING_GROUP_REMAINDER_RATIO = 0.5


def _classify_distance_shape(sorted_distances: list[float]) -> str:
    """The pure classification `_match_shape_note` wraps -- takes already
    -sorted-ascending `vector_distance` values directly (never a
    `ProductMatch`), specifically so it can be tested against a literal
    list of numbers without constructing a full row for each one. This is
    also exactly the seam a reviewer (or Phase 6) would want to call this
    logic through directly, the same way they already did against both
    of this module's earlier versions.

    **Why a leading GROUP, not a leading item (fix round 2).** The first
    version compared only the best result to the second-best, which
    called `[0.13, 0.15, 0.70, 0.75, ...]` -- two genuinely close matches
    sitting above a real noise tail -- "flat", and then told the model
    "nothing here stands out" about a set where two results plainly do.
    That is `docs/PHASE-5.md` §3's own named case ("Camry LE vs Camry SE")
    and a materially worse failure than the uninformative constant note
    this whole mechanism replaced: a note that asserts something false
    reads as a finding, where silence at least does not mislead.

    **Why "at most twice the remainder", not "at most half the set" (fix
    round 3).** Fix round 2's fix capped a candidate leading group at
    `len // 2` -- a group cannot be the majority of the set. That cap
    itself had the SAME bug one level down: at `n = 10`, nine near-tied
    results plus one trailing outlier correctly stayed FLAT (a group of
    nine is excluded, over the `n // 2 = 5` cap), but at `n = 3`, a group
    of two (two genuine matches, one real outlier -- `[0.10, 0.12, 0.90]`)
    was ALSO excluded (`n // 2 = 1`), so the note wrongly claimed "nothing
    stands out" about a set where two results plainly did -- the identical
    class of false statement fix round 2 exists to prevent, surviving at a
    size (`search_products` with a small `limit`, or a filtered catalogue)
    that is entirely ordinary. A cap expressed as a FRACTION OF THE WHOLE
    SET cannot be right at both a large and a small `n` in this way; what
    actually needs to be true is that the group leaves behind a REMAINDER
    substantial enough, relative to the group's OWN size, to plausibly
    call the group "leading" it -- not that the group is outright a
    minority of everything. `boundary * _LEADING_GROUP_REMAINDER_RATIO <=
    remainder` (equivalently: the group is at most twice the remainder)
    is that single rule, with no separate size regime for small `n`: it
    is what lets `n = 3`'s group of two (remainder one, exactly at the
    line) through while still excluding `n = 10`'s group of nine
    (remainder one, nowhere close for a group that size).

    **Fix round 4: this boundary rule still has an edge, and that is why
    `_FLAT_BAND_NOTE` no longer claims "nothing stands out" (see that
    constant's own comment).** At `n = 7`, a leading group of five
    (remainder two: `5 * 0.5 = 2.5 > 2`) is excluded by the SAME
    reasoning that correctly excludes a group of nine at `n = 10` --
    which is correct FOR THIS FUNCTION'S JOB (deciding whether a gap is
    admissible as "the" leading boundary), but means this function
    returning `False`-shaped ("no admissible boundary had a big enough
    gap") does not mean "no group of any size separated", only "no group
    this function was willing to call a genuine leader did." The caller
    (`_FLAT_BAND_NOTE`) is written to be true of that narrower fact, not
    the stronger one this function cannot actually support. Chasing the
    boundary itself for a fourth round -- widening
    `_LEADING_GROUP_REMAINDER_RATIO` further -- would only relocate the
    same edge again; there is no value of it that admits every real
    leading group while excluding every spurious one, because "how large
    can a leading group be" is a judgement call, not a fact recoverable
    from the distances.

    Checks every candidate boundary the remainder rule admits and keeps
    whichever has the biggest gap relative to the set's total spread. A
    single clear leader is still just the `group_size == 1` case of this
    same loop (a remainder of `n - 1` always clears the bar) -- nothing
    about the sharp/flat classification for that shape changes.
    """
    total_spread = sorted_distances[-1] - sorted_distances[0]
    if total_spread <= 1e-9:
        # Every embedded result is (near enough) equidistant from the
        # query -- the flattest possible band, and not merely "ambiguous":
        # there IS enough data here, and what it shows is that nothing
        # stands out. Guards against a bare `== 0.0` check being defeated
        # by float noise from postgres/pgvector's own arithmetic, without
        # smuggling in a distance-scale-dependent tolerance -- 1e-9 is
        # being used as "indistinguishable from zero", not as a threshold
        # on what the distances themselves mean.
        return _FLAT_BAND_NOTE

    sample_size = len(sorted_distances)
    best_gap_ratio = 0.0
    for boundary in range(1, sample_size):
        remainder = sample_size - boundary
        if boundary * _LEADING_GROUP_REMAINDER_RATIO > remainder:
            # This group would outnumber its own remainder by more than
            # `_LEADING_GROUP_REMAINDER_RATIO` allows -- not a leading
            # group, just most of the list with a few stragglers, and
            # every LARGER boundary only shrinks the remainder further, so
            # nothing past this point can pass either.
            break
        gap = sorted_distances[boundary] - sorted_distances[boundary - 1]
        best_gap_ratio = max(best_gap_ratio, gap / total_spread)

    if best_gap_ratio >= _SHARP_LEADER_GAP_RATIO:
        return _SHARP_LEADER_NOTE
    return _FLAT_BAND_NOTE


def _match_shape_note(matches: list[ProductMatch]) -> str:
    """Which of the three notes above describes this returned set's own
    `vector_distance` values -- self-referentially, with no cross-embedder
    calibration, because nothing here is ever compared to anything but the
    SAME set's own numbers (see this module's docstring and
    `_classify_distance_shape`, which does the actual classification)."""
    distances = sorted(m.vector_distance for m in matches if m.vector_distance is not None)
    if len(distances) < _MIN_SHAPE_SAMPLE:
        return _AMBIGUOUS_MATCH_QUALITY_NOTE
    return _classify_distance_shape(distances)


class SearchProductsArgs(BaseModel):
    query: str | None = Field(
        default=None,
        description="A focused search query for the product catalogue -- rewrite the "
        "user's question into a standalone description of what they want, resolving "
        "pronouns against the conversation. Omit to browse by filters alone (e.g. "
        "'everything under £30,000' has no similarity query, only a price filter).",
    )
    category: str | None = Field(default=None, description="Exact category to filter to.")
    min_price: Decimal | None = Field(
        default=None, description="Minimum price (inclusive), in the catalogue's own currency."
    )
    max_price: Decimal | None = Field(
        default=None, description="Maximum price (inclusive), in the catalogue's own currency."
    )
    attributes: dict[str, Any] | None = Field(
        default=None,
        description='Exact attribute filters, e.g. {"seats": 7} -- matches a product '
        "whose own attributes are a superset of this.",
    )
    limit: int = Field(default=10, description="Maximum number of products to return.")


class GetProductArgs(BaseModel):
    product_id: uuid.UUID = Field(description="The product's id, from a prior search result.")


def _format_price(price: Decimal | None, currency: str | None) -> str:
    if price is None:
        return "price not set"
    return f"{price} {currency}" if currency else str(price)


def _match_signal_line(match: ProductMatch) -> str:
    vector = f"{match.vector_distance:.4f}" if match.vector_distance is not None else "n/a"
    keyword = f"{match.keyword_rank:.4f}" if match.keyword_rank is not None else "n/a"
    return f"   Match signal: vector_distance={vector}, keyword_rank={keyword}"


def _render_common(
    *,
    name: str,
    price: Decimal | None,
    currency: str | None,
    availability: ProductAvailability,
    stock_quantity: int | None,
    category: str | None,
    attributes: dict[str, Any],
    description: str | None,
) -> list[str]:
    lines = [f"   Price: {_format_price(price, currency)}"]
    availability_line = f"   Availability: {availability.value}"
    if stock_quantity is not None:
        availability_line += f" ({stock_quantity} in stock)"
    lines.append(availability_line)
    if category:
        lines.append(f"   Category: {category}")
    if attributes:
        lines.append(f"   Attributes: {json.dumps(attributes, sort_keys=True)}")
    if description:
        lines.append(f"   Description: {description}")
    return lines


def _render_match(rank: int, match: ProductMatch, *, ranked: bool) -> str:
    lines = [f"{rank}. {match.name} (source: product:{match.product_id})"]
    lines.extend(
        _render_common(
            name=match.name,
            price=match.price,
            currency=match.currency,
            availability=match.availability,
            stock_quantity=match.stock_quantity,
            category=match.category,
            attributes=match.attributes,
            description=match.description,
        )
    )
    if ranked:
        lines.append(_match_signal_line(match))
    return "\n".join(lines)


def _match_data(match: ProductMatch) -> dict[str, Any]:
    return {
        "source": f"product:{match.product_id}",
        "product_id": str(match.product_id),
        "external_id": match.external_id,
        "name": match.name,
        "description": match.description,
        "category": match.category,
        "price": str(match.price) if match.price is not None else None,
        "currency": match.currency,
        "attributes": match.attributes,
        "availability": match.availability.value,
        "stock_quantity": match.stock_quantity,
        "image_url": match.image_url,
        "product_url": match.product_url,
        "vector_distance": match.vector_distance,
        "keyword_rank": match.keyword_rank,
    }


def _match_citation(match: ProductMatch, rank: int) -> CitationPayload:
    return CitationPayload(
        chunk_id=None,
        document_id=None,
        product_id=match.product_id,
        document_title=match.name,
        rank=rank,
        score=match.score,
        excerpt=f"{_format_price(match.price, match.currency)} · {match.availability.value}",
        page=None,
    )


def _product_data(product: Product) -> dict[str, Any]:
    return {
        "source": f"product:{product.id}",
        "product_id": str(product.id),
        "external_id": product.external_id,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "price": str(product.price) if product.price is not None else None,
        "currency": product.currency,
        "attributes": product.attributes,
        "availability": product.availability.value,
        "stock_quantity": product.stock_quantity,
        "image_url": product.image_url,
        "product_url": product.product_url,
    }


def _product_citation(product: Product) -> CitationPayload:
    # `score`/`rank` are the query-less-search convention from
    # `app/rag/products.py::_search_filtered` (`0.0`, rank `1`): a direct
    # fetch by id has no ranking signal to report any more than a
    # filters-only browse does.
    return CitationPayload(
        chunk_id=None,
        document_id=None,
        product_id=product.id,
        document_title=product.name,
        rank=1,
        score=0.0,
        excerpt=f"{_format_price(product.price, product.currency)} · {product.availability.value}",
        page=None,
    )


def _render_product(product: Product) -> str:
    lines = [f"{product.name} (source: product:{product.id})"]
    lines.extend(
        _render_common(
            name=product.name,
            price=product.price,
            currency=product.currency,
            availability=product.availability,
            stock_quantity=product.stock_quantity,
            category=product.category,
            attributes=product.attributes,
            description=product.description,
        )
    )
    return "\n".join(lines)


class SearchProductsTool(AgentTool):
    name = "search_products"
    description = (
        "Search the organization's product catalogue by keyword, semantic similarity, "
        "and/or exact filters (category, price range, attributes). Use this -- never a "
        "document chunk -- to answer what the organization sells and what it costs. "
        "Returns structured product data (price, currency, availability, attributes), "
        "each result naming the exact product row it came from, or an explicit message "
        "when nothing matches the filters. Results are ranked, not filtered, by "
        "relevance: check each result's match-quality signal before treating it as "
        "clearly relevant."
    )
    args_model = SearchProductsArgs

    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider | None = None) -> None:
        # Same idiom as `RetrieveKnowledgeTool`: the caller's own,
        # already tenant-bound session, not one this tool opens for itself.
        self.session = session
        self.embedder = embedder

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SearchProductsArgs)
        limit = max(1, min(args.limit, _MAX_LIMIT))
        tenant = TenantContext(
            organization_id=ctx.organization_id,
            user_id=None,
            role=None,
            request_id=ctx.request_id,
        )

        # See `RetrieveKnowledgeTool.execute`'s docstring for why this
        # savepoint exists verbatim: `self.session` is shared across every
        # tool call this turn makes, and a DB-level failure here must not
        # poison the rest of the turn's transaction.
        async with self.session.begin_nested():
            matches = await ProductSearchService(self.session, tenant, self.embedder).search(
                args.query,
                category=args.category,
                min_price=args.min_price,
                max_price=args.max_price,
                attributes=args.attributes,
                limit=limit,
            )

        if not matches:
            return ToolResult(content=_NO_RESULTS_MESSAGE)

        ranked = args.query is not None
        blocks = [
            _render_match(rank, match, ranked=ranked) for rank, match in enumerate(matches, start=1)
        ]
        content = "\n\n".join(blocks)
        if ranked:
            content = f"{_match_shape_note(matches)}\n\n{content}"

        return ToolResult(
            content=content,
            data={"products": [_match_data(match) for match in matches]},
            citations=[_match_citation(match, rank) for rank, match in enumerate(matches, start=1)],
        )


class GetProductTool(AgentTool):
    name = "get_product"
    description = (
        "Fetch the full record for one product by its id -- price, currency, "
        "availability, stock, attributes and description. Use this to confirm or "
        "expand on a product `search_products` already surfaced, never a document "
        "chunk, for any fact this tool can report."
    )
    args_model = GetProductArgs

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GetProductArgs)
        tenant = TenantContext(
            organization_id=ctx.organization_id,
            user_id=None,
            role=None,
            request_id=ctx.request_id,
        )

        try:
            async with self.session.begin_nested():
                product = await ProductService(self.session, tenant).get(args.product_id)
        except NotFoundError:
            # `ProductService.get` raises the identical `NotFoundError`
            # whether `product_id` genuinely does not exist or belongs to
            # another organization (its own docstring: "cross-tenant
            # lookups fail the same way a nonexistent id does") -- so this
            # one message is already the same for both cases, by
            # construction, not by a check added here. Two-layer tenancy
            # (`docs/ARCHITECTURE.md` §2.3) means RLS would already hide a
            # foreign row from the underlying SELECT even if this explicit
            # `organization_id` predicate were ever removed; this is Layer 1
            # on top of it, not instead of it.
            return ToolResult(content=_NOT_FOUND_MESSAGE, is_error=True)

        return ToolResult(
            content=_render_product(product),
            data={"product": _product_data(product)},
            citations=[_product_citation(product)],
        )
