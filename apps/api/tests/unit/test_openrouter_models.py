"""The model list behind the dashboard's model picker.

Nothing here touches the network: `_fetch_payload` is monkeypatched, which is
the whole reason parsing, caching and the fallback are separate functions.
"""

import pytest

from app.llm import openrouter_models as om

pytestmark = pytest.mark.anyio


def _entry(model_id: str, **overrides) -> dict:
    entry = {
        "id": model_id,
        "name": model_id.split("/")[-1],
        "context_length": 262_144,
        "reasoning": {"mandatory": False},
    }
    entry.update(overrides)
    return entry


@pytest.fixture(autouse=True)
def _clean_cache():
    om.reset_cache()
    yield
    om.reset_cache()


def test_only_free_models_are_offered():
    """The picker exists to spend nothing. A paid id in the dropdown is a
    charge one click away."""
    options = om.parse_free_models({"data": [_entry("vendor/model:free"), _entry("vendor/model")]})
    assert [o.id for o in options] == ["vendor/model:free"]


def test_a_model_that_cannot_disable_reasoning_is_dropped():
    """`OpenRouterProvider` sends `reasoning.enabled=false` unconditionally, and
    an endpoint declaring `reasoning.mandatory` answers that with
    `400 Reasoning is mandatory for this endpoint and cannot be disabled`
    (verified live against `liquid/lfm-2.5-2.6b:free`). Offering one would put a
    model in the dropdown that fails on every single message."""
    options = om.parse_free_models(
        {
            "data": [
                _entry("vendor/ok:free"),
                _entry("vendor/mandatory:free", reasoning={"mandatory": True}),
            ]
        }
    )
    assert [o.id for o in options] == ["vendor/ok:free"]


def test_malformed_entries_are_skipped_rather_than_failing_the_whole_list():
    """One unrecognised record must not empty the dropdown -- this payload is a
    third party's, and it gains fields without warning."""
    options = om.parse_free_models(
        {"data": [_entry("vendor/ok:free"), {"no_id": True}, "not-an-object", None]}
    )
    assert [o.id for o in options] == ["vendor/ok:free"]


def test_a_payload_that_is_not_a_model_list_yields_nothing_instead_of_raising():
    assert om.parse_free_models({"unexpected": "shape"}) == []
    assert om.parse_free_models("nonsense") == []


def test_models_are_sorted_so_the_dropdown_order_is_stable():
    """OpenRouter returns them newest-first, so without this the options
    reshuffle under the user between page loads."""
    options = om.parse_free_models(
        {"data": [_entry("b/two:free"), _entry("a/one:free"), _entry("c/three:free")]}
    )
    assert [o.id for o in options] == ["a/one:free", "b/two:free", "c/three:free"]


async def test_the_list_is_cached_so_a_page_load_does_not_hit_openrouter(monkeypatch):
    calls = 0

    async def _fake_fetch():
        nonlocal calls
        calls += 1
        return {"data": [_entry("vendor/ok:free")]}

    monkeypatch.setattr(om, "_fetch_payload", _fake_fetch)
    await om.get_free_models()
    await om.get_free_models()
    assert calls == 1


async def test_the_cache_expires_so_a_retired_model_stops_being_offered(monkeypatch):
    calls = 0

    async def _fake_fetch():
        nonlocal calls
        calls += 1
        return {"data": [_entry("vendor/ok:free")]}

    clock = 1000.0
    monkeypatch.setattr(om, "_fetch_payload", _fake_fetch)
    monkeypatch.setattr(om, "_now", lambda: clock)
    await om.get_free_models()
    clock += om.CACHE_TTL_SECONDS + 1
    await om.get_free_models()
    assert calls == 2


async def test_a_failed_fetch_falls_back_to_a_built_in_list(monkeypatch):
    """OpenRouter being unreachable must not leave the agent form with an empty
    model dropdown and no way to save."""

    async def _boom():
        raise RuntimeError("openrouter is down")

    monkeypatch.setattr(om, "_fetch_payload", _boom)
    options = await om.get_free_models()
    assert options == list(om.FALLBACK_MODELS)
    assert options != []


async def test_an_empty_upstream_list_falls_back_too(monkeypatch):
    """A 200 carrying no usable model is the same problem as no 200 at all."""

    async def _empty():
        return {"data": []}

    monkeypatch.setattr(om, "_fetch_payload", _empty)
    assert await om.get_free_models() == list(om.FALLBACK_MODELS)


def test_every_fallback_model_is_free():
    """The fallback is hardcoded, so nothing but this test stops a paid id
    being pasted into it."""
    assert [o for o in om.FALLBACK_MODELS if not o.id.endswith(":free")] == []
