"""Deterministic scorers for Phase 6 evaluation cases (docs/PHASE-6.md §4).

Pure functions on plain data -- no I/O, no database session, no LLM calls,
and deliberately no import of `app.llm` or the DB layer. `runner.py` (Task
4) is what turns a `ChatService.send` turn into an `Observation` and an
`EvalCase` into `Expectations`; this module only ever sees those two shapes,
which is what keeps it trivially unit-testable and safe to run inside the
worker without a session.

Each scorer answers one row of the §4 table and returns `None` when its
expectation is empty ("not applicable" -- left out of the case and the run's
means, per spec) rather than a vacuous pass. `case_passed` is the other half
of that same rule: a case with zero applicable scorers is not "passed by
default", and neither is one whose turn errored even if every scorer that
did run happened to pass -- an error means there was nothing to score.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any, Literal

# NFKC first, so a full-width character (e.g. a fullwidth-Latin brand name)
# folds to its ASCII form before anything else runs -- casefolding alone
# does not do that normalisation.
_THOUSANDS_SEPARATOR = re.compile(r"(?<=\d),(?=\d{3})")


@dataclass(frozen=True, slots=True)
class ObservedToolCall:
    """One tool call as it actually happened during an eval turn, mirroring
    `MessageToolCall`'s columns (`app/chat/service.py`) -- `content` is the
    tool's FULL result, not the excerpt a citation shows."""

    name: str
    arguments: dict[str, Any]
    is_error: bool
    content: str


@dataclass(frozen=True, slots=True)
class Observation:
    """What one eval case's turn actually produced, extracted from
    `ChatService.send`'s events by `runner.py` (Task 4)."""

    answer: str
    error: str | None
    tool_calls: list[ObservedToolCall]
    cited_document_ids: list[uuid.UUID]
    cited_product_ids: list[uuid.UUID]


@dataclass(frozen=True, slots=True)
class Expectations:
    """What an `EvalCase` expects, in the shape the scorers below consume."""

    reference_answer: str | None
    required_phrases: list[str]
    expected_tool_names: list[str]
    expected_document_ids: list[uuid.UUID]
    expected_product_ids: list[uuid.UUID]


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """One scorer's outcome. `score` is `None` only when `status ==
    "error"` (the judge scorer failing outright) -- every deterministic
    scorer in this module always has a numeric score once it applies at
    all. `detail` must stay JSON-serialisable (it is written straight into
    `EvalResult.scores`), which is why ids are rendered as `str`, not
    `uuid.UUID`.
    """

    score: float | None
    passed: bool
    status: Literal["scored", "error"]
    detail: dict[str, Any]


def normalize(text: str) -> str:
    """Fold `text` down to the comparison form spec §4 defines: Unicode
    NFKC, casefold, thousands separators between digits removed, every
    remaining non-alphanumeric character turned into a space, whitespace
    collapsed, and the result stripped.

    Order matters: NFKC before casefold so width/compatibility forms are
    normalized first; the thousands-separator removal before the "non-
    alphanumeric -> space" pass, since that pass would otherwise already
    have turned every `,` into a space, leaving no comma left to match.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    folded = _THOUSANDS_SEPARATOR.sub("", folded)
    spaced = "".join(ch if ch.isalnum() else " " for ch in folded)
    return " ".join(spaced.split())


def _phrase_matches(phrase: str, normalized_answer_padded: str) -> bool:
    """Word-boundary match by padding both sides with a space: `f" {phrase}
    " in f" {answer} "`. Padding (rather than a regex `\\b`) is what makes
    "3 years" fail to match inside "13 years" -- `\\b` sits at a
    digit/non-digit transition, which "1" -> "3" is not, so a naive regex
    `\\b` approach would (wrongly) match here.
    """
    return f" {normalize(phrase)} " in normalized_answer_padded


def score_required_phrases(exp: Expectations, obs: Observation) -> ScoreResult | None:
    if not exp.required_phrases:
        return None
    padded_answer = f" {normalize(obs.answer)} "
    missing = [p for p in exp.required_phrases if not _phrase_matches(p, padded_answer)]
    found = len(exp.required_phrases) - len(missing)
    return ScoreResult(
        score=found / len(exp.required_phrases),
        passed=not missing,
        status="scored",
        detail={"missing": missing},
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def score_tool_selection(exp: Expectations, obs: Observation) -> ScoreResult | None:
    """Counts only successful calls (`is_error=False`) -- a tool call that
    errored did not answer anything, per spec §4. Tools called that were
    not expected are reported in `detail["unexpected"]` (deduped, first-seen
    order) but never fail the case: looking something up an extra time is
    not wrong.
    """
    if not exp.expected_tool_names:
        return None
    called_successfully = [c.name for c in obs.tool_calls if not c.is_error]
    called_set = set(called_successfully)
    expected_set = set(exp.expected_tool_names)
    missing = [name for name in exp.expected_tool_names if name not in called_set]
    unexpected = _dedupe([name for name in called_successfully if name not in expected_set])
    found = len(exp.expected_tool_names) - len(missing)
    return ScoreResult(
        score=found / len(exp.expected_tool_names),
        passed=not missing,
        status="scored",
        detail={"missing": missing, "unexpected": unexpected},
    )


def _score_recall(expected: list[uuid.UUID], cited: list[uuid.UUID]) -> ScoreResult | None:
    if not expected:
        return None
    cited_set = set(cited)
    missing = [str(i) for i in expected if i not in cited_set]
    found = len(expected) - len(missing)
    return ScoreResult(
        score=found / len(expected),
        passed=not missing,
        status="scored",
        detail={"missing": missing},
    )


def score_document_recall(exp: Expectations, obs: Observation) -> ScoreResult | None:
    return _score_recall(exp.expected_document_ids, obs.cited_document_ids)


def score_product_recall(exp: Expectations, obs: Observation) -> ScoreResult | None:
    return _score_recall(exp.expected_product_ids, obs.cited_product_ids)


def deterministic_scores(exp: Expectations, obs: Observation) -> dict[str, ScoreResult]:
    """Every applicable deterministic scorer, keyed exactly as
    `EvalResult.scores` (and the judge's own key, added by `runner.py`)
    stores them. Not-applicable scorers are left out entirely, per spec §4.
    """
    scores: dict[str, ScoreResult] = {}
    for key, result in (
        ("required_phrases", score_required_phrases(exp, obs)),
        ("tool_selection", score_tool_selection(exp, obs)),
        ("document_recall", score_document_recall(exp, obs)),
        ("product_recall", score_product_recall(exp, obs)),
    ):
        if result is not None:
            scores[key] = result
    return scores


def case_passed(scores: dict[str, ScoreResult], obs: Observation) -> bool:
    """A case passes when the turn did not error, at least one scorer ran,
    and every scorer that ran passed. An empty `scores` dict (no applicable
    expectation at all) does not pass by default -- there is nothing here
    that measured anything.
    """
    if obs.error is not None:
        return False
    if not scores:
        return False
    return all(result.passed for result in scores.values())


def score_to_json(result: ScoreResult) -> dict[str, Any]:
    return {
        "score": result.score,
        "passed": result.passed,
        "status": result.status,
        "detail": result.detail,
    }
