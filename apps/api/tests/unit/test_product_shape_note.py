"""`_match_shape_note`/`_classify_distance_shape` (`app/tools/products.py`),
tested directly against literal `vector_distance` values -- the exact seam
a reviewer used to find the bug across four rounds:

- Fix round 1 compared only the best result to the second-best, so
  `[0.13, 0.15, 0.70, ...]` (two genuinely close matches sitting above a
  real tail -- `docs/PHASE-5.md` §3's own named case, "Camry LE vs Camry
  SE") read as "flat", telling the model nothing stood out about a set
  where two results plainly did.
- Fix round 2 fixed that by generalising to a leading GROUP, capped at
  half the set (`len // 2`) so a group could never be a majority. That cap
  had the SAME bug one size down: `[0.10, 0.12, 0.90]` (`n = 3`, a group of
  two excluded by `n // 2 = 1`) read as "flat" for the identical reason,
  at a set size (`search_products` with a small `limit`, or a filtered
  catalogue) that is entirely ordinary.
- Fix round 3 replaced the majority cap with a single rule that scales
  correctly at every `n` tried so far: a candidate leading group is only
  considered when it does not outnumber its own remainder by more than
  `_LEADING_GROUP_REMAINDER_RATIO` allows (`boundary *
  _LEADING_GROUP_REMAINDER_RATIO <= remainder`).
- A wider sweep (`n = 2..12`) found the SAME class of bug again: at
  `n = 7`, a leading group of five (remainder two) is excluded by that
  same rule (`5 * 0.5 = 2.5 > 2`), so five genuinely good results above
  two bad ones still read as "flat". Fix round 4 stops trying to widen
  the boundary rule -- "how large can a leading group be before it stops
  leading" has no principled cutoff, so every rule will have an edge a
  wider sweep finds -- and instead weakens `_FLAT_BAND_NOTE` itself to
  assert only what this branch actually establishes: no SINGLE result
  clearly separates from the rest. That is true in every shape below,
  including the `n = 7` one, where it is weaker than what a human
  reading the numbers could see, deliberately.

Pure numbers, not a `HashingEmbedder` corpus, and in `tests/unit`, not
`tests/integration`: fix round 2 established that a corpus built from
perfect ties at exactly 1.0 is a degenerate trap, and separately that
`tests/integration/conftest.py`'s autouse, event-loop-requiring
`_dispose_shared_engine` fixture corrupts pytest's own fixture-teardown
bookkeeping when a plain synchronous test shares a module with it (a
confirmed, recorded infrastructure hazard, not something this file works
around by coincidence).

Built through `_match_shape_note` and a minimal `ProductMatch` factory,
not `_classify_distance_shape` directly: `_classify_distance_shape` alone
has no defined behaviour below `_MIN_SHAPE_SAMPLE` results (that gate lives
in `_match_shape_note`), and one of the shapes fix round 3 must cover --
a two-element set -- only exists on the ACTUAL production seam, not the
inner one.

See `tests/integration/test_product_tools.py`'s own note for where the
same shape is proven end to end, through a real embedder and the real
tool -- this file owns the classification boundary itself, that file owns
the wiring around it.
"""

import uuid

from app.db.models import ProductAvailability
from app.rag.products import ProductMatch
from app.tools.products import (
    _AMBIGUOUS_MATCH_QUALITY_NOTE,
    _FLAT_BAND_NOTE,
    _SHARP_LEADER_NOTE,
    _match_shape_note,
)


def _match(vector_distance: float | None) -> ProductMatch:
    """A `ProductMatch` carrying only the one field `_match_shape_note`
    reads -- every other field is an otherwise-irrelevant placeholder.
    Constructed directly, with no database round trip, which is the whole
    point of testing this seam here: the classification boundary is pure
    arithmetic over `vector_distance` values, and deserves a test suite
    that treats it as exactly that."""
    return ProductMatch(
        product_id=uuid.uuid4(),
        external_id="sku",
        name="Product",
        description=None,
        category=None,
        price=None,
        currency=None,
        attributes={},
        availability=ProductAvailability.IN_STOCK,
        stock_quantity=None,
        image_url=None,
        product_url=None,
        score=0.0,
        rank=1,
        vector_distance=vector_distance,
        keyword_rank=None,
    )


def _shape_note(distances: list[float]) -> str:
    return _match_shape_note([_match(d) for d in distances])


# ---------------------------------------------------------------------------
# The two regression examples, at the two sizes each was found at.
# ---------------------------------------------------------------------------


def test_two_near_tied_leaders_above_a_noise_tail_is_sharp_not_flat() -> None:
    """The reviewer's original regression example (fix round 1, `n = 10`).
    Two results (0.13, 0.15) sit far below a real tail (0.70 up to 1.00).
    Fix round 1's rank-1-vs-rank-2-only comparison called this "flat";
    pinned here so a regression back to that comparison fails this test
    directly, not just a corpus-dependent integration test."""
    distances = [0.13, 0.15, 0.70, 0.75, 0.79, 0.85, 0.91, 0.92, 0.93, 1.00]
    assert _shape_note(distances) == _SHARP_LEADER_NOTE


def test_two_genuine_matches_above_one_outlier_is_sharp_at_small_n() -> None:
    """The reviewer's fix round 3 regression example: the identical shape
    as the test above, at `n = 3`. Fix round 2's `len // 2` cap excluded a
    group of two here (`3 // 2 = 1`), reproducing fix round 1's exact
    class of false statement at a size that is entirely ordinary for
    `search_products` (a small `limit`, or a filtered catalogue) rather
    than a corner case."""
    distances = [0.10, 0.12, 0.90]
    assert _shape_note(distances) == _SHARP_LEADER_NOTE


def test_three_genuine_matches_above_two_outliers_is_sharp_at_small_n() -> None:
    """The same regression at `n = 5`: a group of three, correctly excluded
    from consideration under fix round 2's cap (`5 // 2 = 2`), now
    correctly included (`boundary=3` leaves a remainder of 2, and
    `3 * 0.5 = 1.5 <= 2`)."""
    distances = [0.10, 0.11, 0.12, 0.85, 0.90]
    assert _shape_note(distances) == _SHARP_LEADER_NOTE


def test_a_lone_trailing_outlier_does_not_make_the_rest_a_leading_group() -> None:
    """Nine near-tied results (a flat cluster on their own) followed by one
    outlier far from all of them. A group of nine leaves a remainder of
    only one (`9 * 0.5 = 4.5 > 1`), so fix round 3's remainder rule
    excludes it exactly as fix round 2's majority cap did -- this is the
    shape that rule exists to keep FLAT, and it must stay FLAT under the
    new rule too."""
    distances = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.90]
    assert _shape_note(distances) == _FLAT_BAND_NOTE


def test_five_good_results_above_two_bad_ones_gets_a_note_true_of_that_shape() -> None:
    """Fix round 4's own regression shape (`n=7`, a leading group of five,
    two stragglers): the SAME rule that correctly excludes a group of nine
    at `n=10` also excludes a group of five here (`5 * 0.5 = 2.5 > 2`,
    the remainder of two is not large enough), so this still classifies
    FLAT -- and that is fine, because fix round 4's point is not that the
    verdict changes, it is that the WORDING must stay true regardless of
    which verdict this shape gets. Fix round 3's wording ("nothing stands
    out") would have been false here: five results plainly do. Fix round
    4's wording ("no SINGLE result clearly separates") is true regardless
    -- none of the five distinguishes itself from its four siblings, even
    though the group of five collectively separates from the two."""
    distances = [0.10, 0.11, 0.12, 0.13, 0.14, 0.80, 0.82]
    note = _shape_note(distances)
    assert note == _FLAT_BAND_NOTE
    assert "nothing" not in note.lower()
    assert "no result" not in note.lower()  # would falsely include the group of five
    assert "single result" in note.lower()


# ---------------------------------------------------------------------------
# Task 4's own measured distributions.
# ---------------------------------------------------------------------------


def test_the_measured_single_leader_shape_is_sharp() -> None:
    """Task 4's own measured "genuine match" distribution: 0.1294 for the
    right answer, 0.6932 for the next-closest, tailing to 0.9293
    (`app/rag/products.py`'s module docstring). The intermediate tail
    values are illustrative -- Task 4's own comment records only the two
    closest points and the maximum -- but the classification does not
    depend on them: the dominant gap is the one right after the leader
    (0.5638 of a 0.7999 total spread, ratio 0.7048), comfortably clear of
    every other candidate boundary in this list."""
    distances = [0.1294, 0.6932, 0.75, 0.81, 0.86, 0.9293]
    assert _shape_note(distances) == _SHARP_LEADER_NOTE


def test_the_measured_unrelated_query_shape_is_flat() -> None:
    """Task 4's own measured "wholly unrelated query" distribution: all ten
    rows within 0.9139-1.0000 (`app/rag/products.py`'s module docstring).
    The exact ten values were never individually recorded, only the range
    and the count -- these are representative values spanning that same
    range with irregular (not evenly-stepped) spacing, so this is not a
    suspiciously clean synthetic pattern either."""
    distances = [
        0.9139,
        0.9201,
        0.9280,
        0.9350,
        0.9410,
        0.9500,
        0.9580,
        0.9650,
        0.9800,
        1.0000,
    ]
    assert _shape_note(distances) == _FLAT_BAND_NOTE


def test_the_measured_sharp_and_flat_shapes_produce_different_notes() -> None:
    """The assertion that makes this mechanism worth having, at the level
    it is actually decided: if the classifier reads the same on both of
    Task 4's own measured distributions, nothing about this fix changed
    anything."""
    sharp = [0.1294, 0.6932, 0.75, 0.81, 0.86, 0.9293]
    flat = [0.9139, 0.9201, 0.9280, 0.9350, 0.9410, 0.9500, 0.9580, 0.9650, 0.9800, 1.0000]
    assert _shape_note(sharp) != _shape_note(flat)
    assert _shape_note(sharp) == _SHARP_LEADER_NOTE
    assert _shape_note(flat) == _FLAT_BAND_NOTE


# ---------------------------------------------------------------------------
# The remaining shapes fix round 3 named -- each must produce a note that
# is TRUE of the shape, not necessarily the same note as any other case.
# ---------------------------------------------------------------------------


def test_a_two_element_set_falls_back_to_the_ambiguous_note() -> None:
    """Two results are `_MIN_SHAPE_SAMPLE`'s own floor: there is exactly one
    gap and nothing of its own to compare it against, so `_match_shape_note`
    never even reaches `_classify_distance_shape` for this shape -- the
    honest, and only true, statement is "too few results to tell"."""
    assert _shape_note([0.20, 0.90]) == _AMBIGUOUS_MATCH_QUALITY_NOTE


def test_all_identical_distances_is_flat() -> None:
    """Every result at the exact same distance from the query -- the
    flattest possible band, definitionally true: nothing stands out
    because nothing differs at all."""
    assert _shape_note([0.5, 0.5, 0.5, 0.5]) == _FLAT_BAND_NOTE


def test_a_smooth_monotonic_gradient_is_flat() -> None:
    """Evenly spaced distances with no cluster and no break -- every
    adjacent gap is identical, so no candidate boundary's gap is any more
    "the" leading edge than any other. Calling any one of them a genuine
    leader would be arbitrary, not supported by the data; FLAT ("nothing
    stands out") is the true reading of a shape with no distinguishing
    feature at all."""
    distances = [0.10, 0.26, 0.42, 0.58, 0.74, 0.90]
    assert _shape_note(distances) == _FLAT_BAND_NOTE


def test_a_single_outlier_below_a_tight_cluster_is_sharp() -> None:
    """The mirror image of the trailing-outlier shape above: one genuinely
    close match sitting well below an otherwise tight cluster of much
    worse ones. A group of one always clears the remainder rule (the
    remainder is everything else), so this is the plainest possible
    sharp-leader shape and must stay SHARP under any version of this
    rule."""
    distances = [0.10, 0.85, 0.86, 0.87, 0.88, 0.89]
    assert _shape_note(distances) == _SHARP_LEADER_NOTE
