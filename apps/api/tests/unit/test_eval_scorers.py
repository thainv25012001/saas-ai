"""Unit tests for `app/evaluations/scorers.py` -- pure, deterministic scoring.

See docs/PHASE-6.md §4 and the Task 3 brief for the exact semantics each
test pins: normalisation rules, "not applicable" returning `None`, only
successful tool calls counting toward `tool_selection`, and `case_passed`
failing whenever the turn itself errored regardless of scorer outcomes.
"""

import uuid
from typing import Any

import pytest

from app.evaluations.scorers import (
    Expectations,
    Observation,
    ObservedToolCall,
    ScoreResult,
    case_passed,
    deterministic_scores,
    normalize,
    score_document_recall,
    score_product_recall,
    score_required_phrases,
    score_to_json,
    score_tool_selection,
)


def _expectations(**overrides: Any) -> Expectations:
    defaults: dict[str, Any] = {
        "reference_answer": None,
        "required_phrases": [],
        "expected_tool_names": [],
        "expected_document_ids": [],
        "expected_product_ids": [],
    }
    defaults.update(overrides)
    return Expectations(**defaults)


def _observation(**overrides: Any) -> Observation:
    defaults: dict[str, Any] = {
        "answer": "",
        "error": None,
        "tool_calls": [],
        "cited_document_ids": [],
        "cited_product_ids": [],
    }
    defaults.update(overrides)
    return Observation(**defaults)


class TestNormalize:
    def test_thousands_separator_removed(self) -> None:
        assert normalize("$35,000") == normalize("35000") == "35000"

    def test_hyphen_becomes_space(self) -> None:
        assert normalize("3-year") == normalize("3 year") == "3 year"

    def test_full_width_and_case_insensitive(self) -> None:
        assert normalize("ＣＡＭＲＹ") == normalize("camry") == "camry"

    def test_whitespace_collapsed_and_stripped(self) -> None:
        assert normalize("  hello   world  ") == "hello world"


class TestScoreRequiredPhrases:
    def test_not_applicable_when_no_phrases(self) -> None:
        assert score_required_phrases(_expectations(), _observation()) is None

    def test_dollar_amount_matches_bare_number(self) -> None:
        exp = _expectations(required_phrases=["35000"])
        obs = _observation(answer="The price is $35,000 total.")
        result = score_required_phrases(exp, obs)
        assert result is not None
        assert result.score == 1.0
        assert result.passed is True
        assert result.status == "scored"
        assert result.detail == {"missing": []}

    def test_hyphenated_phrase_matches_spaced_form(self) -> None:
        exp = _expectations(required_phrases=["3 year"])
        obs = _observation(answer="Comes with a 3-year warranty.")
        result = score_required_phrases(exp, obs)
        assert result is not None
        assert result.passed is True

    def test_word_boundary_prevents_substring_match(self) -> None:
        exp = _expectations(required_phrases=["3 years"])
        obs = _observation(answer="Backed by a 13 years guarantee.")
        result = score_required_phrases(exp, obs)
        assert result is not None
        assert result.passed is False
        assert result.score == 0.0
        assert result.detail == {"missing": ["3 years"]}

    def test_diacritic_and_width_insensitive(self) -> None:
        exp = _expectations(required_phrases=["camry"])
        obs = _observation(answer="We recommend the ＣＡＭＲＹ model.")
        result = score_required_phrases(exp, obs)
        assert result is not None
        assert result.passed is True

    def test_partial_match_fraction(self) -> None:
        exp = _expectations(required_phrases=["alpha", "beta", "gamma"])
        obs = _observation(answer="alpha and beta only")
        result = score_required_phrases(exp, obs)
        assert result is not None
        assert result.score == pytest.approx(2 / 3)
        assert result.passed is False
        assert result.detail == {"missing": ["gamma"]}


class TestScoreToolSelection:
    def test_not_applicable_when_no_expected_tools(self) -> None:
        assert score_tool_selection(_expectations(), _observation()) is None

    def test_errored_call_not_counted(self) -> None:
        exp = _expectations(expected_tool_names=["search_products"])
        obs = _observation(
            tool_calls=[
                ObservedToolCall(
                    name="search_products", arguments={}, is_error=True, content="boom"
                )
            ]
        )
        result = score_tool_selection(exp, obs)
        assert result is not None
        assert result.score == 0.0
        assert result.passed is False
        assert result.detail["missing"] == ["search_products"]

    def test_unexpected_tool_reported_but_still_passes(self) -> None:
        exp = _expectations(expected_tool_names=["search_products"])
        obs = _observation(
            tool_calls=[
                ObservedToolCall(
                    name="search_products", arguments={}, is_error=False, content="ok"
                ),
                ObservedToolCall(
                    name="retrieve_knowledge", arguments={}, is_error=False, content="ok"
                ),
            ]
        )
        result = score_tool_selection(exp, obs)
        assert result is not None
        assert result.score == 1.0
        assert result.passed is True
        assert result.detail == {"missing": [], "unexpected": ["retrieve_knowledge"]}

    def test_unexpected_tools_deduped(self) -> None:
        exp = _expectations(expected_tool_names=["search_products"])
        obs = _observation(
            tool_calls=[
                ObservedToolCall(
                    name="search_products", arguments={}, is_error=False, content="ok"
                ),
                ObservedToolCall(
                    name="retrieve_knowledge", arguments={}, is_error=False, content="a"
                ),
                ObservedToolCall(
                    name="retrieve_knowledge", arguments={}, is_error=False, content="b"
                ),
            ]
        )
        result = score_tool_selection(exp, obs)
        assert result is not None
        assert result.detail["unexpected"] == ["retrieve_knowledge"]


class TestRecall:
    def test_document_recall_not_applicable(self) -> None:
        assert score_document_recall(_expectations(), _observation()) is None

    def test_document_recall_fraction(self) -> None:
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        exp = _expectations(expected_document_ids=[a, b, c])
        obs = _observation(cited_document_ids=[a, b])
        result = score_document_recall(exp, obs)
        assert result is not None
        assert result.score == pytest.approx(2 / 3)
        assert result.passed is False
        assert result.detail == {"missing": [str(c)]}

    def test_product_recall_not_applicable(self) -> None:
        assert score_product_recall(_expectations(), _observation()) is None

    def test_product_recall_all_cited_passes(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        exp = _expectations(expected_product_ids=[a, b])
        obs = _observation(cited_product_ids=[b, a])
        result = score_product_recall(exp, obs)
        assert result is not None
        assert result.score == 1.0
        assert result.passed is True
        assert result.detail == {"missing": []}


class TestDeterministicScores:
    def test_only_applicable_scorers_present(self) -> None:
        exp = _expectations(required_phrases=["hello"])
        obs = _observation(answer="hello there")
        scores = deterministic_scores(exp, obs)
        assert set(scores) == {"required_phrases"}

    def test_all_four_when_all_applicable(self) -> None:
        a = uuid.uuid4()
        b = uuid.uuid4()
        exp = _expectations(
            required_phrases=["hi"],
            expected_tool_names=["search_products"],
            expected_document_ids=[a],
            expected_product_ids=[b],
        )
        obs = _observation(
            answer="hi",
            tool_calls=[
                ObservedToolCall(name="search_products", arguments={}, is_error=False, content="ok")
            ],
            cited_document_ids=[a],
            cited_product_ids=[b],
        )
        scores = deterministic_scores(exp, obs)
        assert set(scores) == {
            "required_phrases",
            "tool_selection",
            "document_recall",
            "product_recall",
        }

    def test_empty_when_nothing_applicable(self) -> None:
        assert deterministic_scores(_expectations(), _observation()) == {}


class TestCasePassed:
    def test_true_when_all_pass_and_no_error(self) -> None:
        scores = {
            "required_phrases": ScoreResult(score=1.0, passed=True, status="scored", detail={})
        }
        assert case_passed(scores, _observation(error=None)) is True

    def test_false_when_error_present_even_if_all_pass(self) -> None:
        scores = {
            "required_phrases": ScoreResult(score=1.0, passed=True, status="scored", detail={})
        }
        assert case_passed(scores, _observation(error="the turn errored")) is False

    def test_false_when_any_scorer_fails(self) -> None:
        scores = {
            "required_phrases": ScoreResult(score=1.0, passed=True, status="scored", detail={}),
            "tool_selection": ScoreResult(score=0.0, passed=False, status="scored", detail={}),
        }
        assert case_passed(scores, _observation()) is False

    def test_false_when_no_scores_at_all(self) -> None:
        assert case_passed({}, _observation()) is False


class TestScoreToJson:
    def test_shape(self) -> None:
        result = ScoreResult(score=0.5, passed=False, status="scored", detail={"missing": ["x"]})
        assert score_to_json(result) == {
            "score": 0.5,
            "passed": False,
            "status": "scored",
            "detail": {"missing": ["x"]},
        }
