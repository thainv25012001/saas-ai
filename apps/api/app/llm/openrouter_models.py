"""The list of OpenRouter models this app offers in the dashboard's picker.

Fetched live rather than hardcoded because OpenRouter's free roster turns over
continually -- models are added and retired without notice, so a list written
into the repo is wrong within weeks and its staleness is invisible until
somebody picks a dead id. `https://openrouter.ai/api/v1/models` is public and
needs no credentials.

Two filters apply, and both exist to keep every offered model one that actually
works (see `parse_free_models`). A hardcoded `FALLBACK_MODELS` covers OpenRouter
being unreachable, so the form is never left with an empty dropdown.
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass

# The same library the OpenAI and Anthropic SDKs already use, rather than a
# second HTTP client in the closure for one public GET.
import httpx2 as httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# An hour. The roster changes on the order of days, and this list only feeds a
# dropdown, so an hour-old answer is never wrong in a way a user would notice --
# while still meaning a dashboard left open all day does not re-fetch per render.
CACHE_TTL_SECONDS = 3600.0

_FETCH_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ModelOption:
    id: str
    label: str
    context_length: int | None


# Used only when the live fetch fails. Each was run against the live API on
# 2026-09-16 and answered in prose in under two seconds. Deliberately short: it
# is a safety net, not a mirror of the roster.
#
# Verified by running the adapter, not read off the model list, because many
# free ids are REASONING models: OpenRouter returns their thinking on a
# `reasoning` field the OpenAI-compatible `content` never carries, so they
# answer a one-sentence question with an empty string, `finish_reason="length"`
# and the whole output budget spent (`z-ai/glm-5.2:free` took 80s to return
# nothing). The first entry is also `registry.DEFAULT_MODELS["openrouter"]`, so
# it is the one to re-verify first -- free ids are retired without notice.
FALLBACK_MODELS: tuple[ModelOption, ...] = (
    ModelOption("google/gemma-4-31b-it:free", "Gemma 4 31B", 262_144),
    ModelOption("nex-agi/nex-n2.5-mini:free", "Nex N2.5 Mini", 262_144),
    ModelOption("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super 120B", 262_144),
    ModelOption("poolside/laguna-xs-2.1:free", "Laguna XS 2.1", 262_144),
)

# How long a failed refresh is honoured before trying OpenRouter again. Short,
# because it only delays noticing that OpenRouter came back -- but not zero:
# without it every request during an outage pays the full `_FETCH_TIMEOUT_SECONDS`
# inside the handler to rediscover the same failure.
_FAILURE_TTL_SECONDS = 60.0

# `(expires_at, options)`. Expiry rather than a write timestamp so a successful
# fetch and a failed one can be held for different lengths of time.
_cache: tuple[float, list[ModelOption]] | None = None


def _now() -> float:
    """Indirected so tests can move the clock without sleeping."""
    return time.monotonic()


def reset_cache() -> None:
    """Drop the cached list. Tests call this; nothing in the app does."""
    global _cache
    _cache = None


def _context_length(entry: dict[str, object]) -> int | None:
    value = entry.get("context_length")
    return value if isinstance(value, int) else None


def _label(entry: dict[str, object], model_id: str) -> str:
    name = entry.get("name")
    return name if isinstance(name, str) and name else model_id


def parse_free_models(payload: object) -> list[ModelOption]:
    """Narrow OpenRouter's `/models` response into the options worth offering.

    Two filters, each earned the hard way against the live API:

    `:free` -- the picker's whole purpose is spending nothing, and OpenRouter's
    own suffix is what marks an endpoint free (the same marker
    `app/llm/pricing.py` prices at zero).

    `reasoning.mandatory` -- `OpenRouterProvider` sends `reasoning.enabled=false`
    on every request, because without it most of the free roster spends its
    entire output budget on thinking the OpenAI-compatible `content` never
    carries and streams an empty reply. An endpoint that declares reasoning
    mandatory rejects that flag with a 400, so offering one would put a model in
    the dropdown that fails on every message.

    Never raises: this is a third party's payload and it gains fields without
    warning, so an unrecognised record is skipped and the rest are kept.
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, Iterable) or isinstance(data, (str, bytes)):
        return []

    options: list[ModelOption] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id.endswith(":free"):
            continue
        reasoning = entry.get("reasoning")
        if isinstance(reasoning, dict) and reasoning.get("mandatory") is True:
            continue
        options.append(ModelOption(model_id, _label(entry, model_id), _context_length(entry)))

    # OpenRouter returns newest-first; without a stable order the dropdown
    # reshuffles under the user between page loads.
    options.sort(key=lambda option: option.id)
    return options


async def _fetch_payload() -> object:
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS) as client:
        response = await client.get(OPENROUTER_MODELS_URL)
        response.raise_for_status()
        return response.json()


async def get_free_models() -> list[ModelOption]:
    """The offered models, cached for `CACHE_TTL_SECONDS`.

    A refresh that fails or comes back empty serves the last list OpenRouter
    gave us, or `FALLBACK_MODELS` if it never gave us one -- an empty dropdown
    is a form that cannot be saved, which is a worse failure than a stale list.
    """
    global _cache
    if _cache is not None and _now() < _cache[0]:
        return _cache[1]

    try:
        options = parse_free_models(await _fetch_payload())
    except Exception as exc:  # noqa: BLE001 - any failure here degrades, never propagates
        logger.warning("openrouter_models_fetch_failed", error=str(exc))
        options = []

    if not options:
        # Prefer the last list OpenRouter actually served: real data an hour old
        # beats a literal written months ago. Held for `_FAILURE_TTL_SECONDS`
        # rather than the full TTL, so recovery is noticed within a minute --
        # and rather than not at all, so an outage costs one timed-out fetch a
        # minute instead of one on every request.
        served = _cache[1] if _cache is not None else list(FALLBACK_MODELS)
        _cache = (_now() + _FAILURE_TTL_SECONDS, served)
        return served

    _cache = (_now() + CACHE_TTL_SECONDS, options)
    return options
