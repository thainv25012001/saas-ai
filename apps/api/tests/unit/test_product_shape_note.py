"""`_classify_distance_shape` (`app/tools/products.py`), tested directly
against literal distance lists -- the exact seam a reviewer used to find
fix round 1's bug: comparing only the best result to the second-best
called `[0.13, 0.15, 0.70, ...]` (two genuinely close matches sitting
above a real tail) "flat", telling the model nothing stood out about a
set where two results plainly did.

Pure numbers, not a `HashingEmbedder` corpus, and deliberately in
`tests/unit`, not `tests/integration`: fix round 2's own ruling was
explicit that a corpus built from perfect ties at exactly 1.0 is itself a
degenerate trap, and there is no reason to approximate Task 4's own
measured distributions through an embedder that cannot be asked to
produce one specific float when the numbers are already on record. (A
first attempt at this file lived in `tests/integration`, driven by plain
`def` tests with no fixtures -- `tests/integration/conftest.py`'s autouse
`_dispose_shared_engine` fixture requires an event loop, which a
synchronous, DB-free test never opens, and pytest's own fixture-teardown
bookkeeping broke in a way that failed *other, unrelated* tests in the
same module. Moving the pure tests here, where nothing is async and
nothing is autoused, was the fix -- not narrowing what each test covers.)

See `tests/integration/test_product_tools.py`'s own note for where the
same shape is proven end to end, through a real embedder and the real
tool -- this file owns the classification boundary itself, that file owns
the wiring around it.
"""

from app.tools.products import (
    _FLAT_BAND_NOTE,
    _SHARP_LEADER_NOTE,
    _classify_distance_shape,
)


def test_two_near_tied_leaders_above_a_noise_tail_is_sharp_not_flat() -> None:
    """The reviewer's exact regression example. Two results (0.13, 0.15)
    sit far below a real tail (0.70 up to 1.00) -- `docs/PHASE-5.md` §3's
    own named case, "Camry LE vs Camry SE" -- and fix round 1's
    rank-1-vs-rank-2-only comparison called this "flat", telling the model
    nothing stood out about a set where two results plainly did. Pinned
    here so a regression back to that comparison fails this test directly,
    not just a corpus-dependent integration test."""
    distances = [0.13, 0.15, 0.70, 0.75, 0.79, 0.85, 0.91, 0.92, 0.93, 1.00]
    assert _classify_distance_shape(distances) == _SHARP_LEADER_NOTE


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
    assert _classify_distance_shape(distances) == _SHARP_LEADER_NOTE


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
    assert _classify_distance_shape(distances) == _FLAT_BAND_NOTE


def test_the_measured_sharp_and_flat_shapes_produce_different_notes() -> None:
    """The assertion that makes this mechanism worth having, at the level
    it is actually decided: if the classifier reads the same on both of
    Task 4's own measured distributions, nothing about this fix changed
    anything."""
    sharp = [0.1294, 0.6932, 0.75, 0.81, 0.86, 0.9293]
    flat = [0.9139, 0.9201, 0.9280, 0.9350, 0.9410, 0.9500, 0.9580, 0.9650, 0.9800, 1.0000]
    assert _classify_distance_shape(sharp) != _classify_distance_shape(flat)
    assert _classify_distance_shape(sharp) == _SHARP_LEADER_NOTE
    assert _classify_distance_shape(flat) == _FLAT_BAND_NOTE


def test_a_lone_trailing_outlier_does_not_make_the_rest_a_leading_group() -> None:
    """Nine near-tied results (a flat cluster on their own) followed by one
    outlier far from all of them. Without a cap on how large a "leading
    group" is allowed to be, this cluster of NINE would register as
    "leading" the one outlier -- misusing `_SHARP_LEADER_NOTE`'s own
    wording ("the top result(s) ... stand out"), which means a genuine
    minority, not nine-tenths of the returned set. `_classify_distance_shape`'s
    `max_group_size = len(sorted_distances) // 2` -- derived from the
    set's own size, not a second tuned constant -- is what keeps this
    FLAT: a leading group cannot be the majority of the list and still be
    leading a tail."""
    distances = [0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.22, 0.24, 0.26, 0.90]
    assert _classify_distance_shape(distances) == _FLAT_BAND_NOTE
