import logging
from contextvars import ContextVar

import structlog
from structlog.typing import EventDict, WrappedLogger

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def _add_request_id(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    request_id = request_id_var.get()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        # NOT cache_logger_on_first_use=True. structlog's `BoundLoggerLazyProxy`
        # caches by permanently rebinding a proxy's `.bind` to a closure that
        # captured `_CONFIG.default_processors` *by reference* at first use.
        # This call reassigns that attribute to a brand-new list every time
        # (Python's `structlog.configure` does not mutate the existing list in
        # place) -- `app.workers.settings._on_startup` calls this again, after
        # import time, specifically so the worker process picks up real
        # configuration instead of structlog's defaults (see that module's own
        # docstring on exactly this hazard, one layer up). Caching would freeze
        # any module logger already warmed before that second call onto the
        # orphaned list, silently deaf to every processor change from then on --
        # in production, not just in a test process. Disabling the cache costs a
        # dict lookup per log call; it buys the ability to reconfigure logging
        # at runtime, which this codebase does in two places
        # (`create_app()` and `_on_startup`).
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
