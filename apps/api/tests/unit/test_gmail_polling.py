from __future__ import annotations

import pytest

from app.gmail.polling import PollFanOut, enqueue_mailbox_polls, gmail_poll_job_id

pytestmark = pytest.mark.anyio


class _FakeArq:
    """arq's enqueue contract: None when the job ID already exists."""

    def __init__(self) -> None:
        self.job_ids: set[str] = set()
        self.calls: list[tuple[str, str, str | None, dict[str, object]]] = []

    async def enqueue_job(
        self, function: str, *, _job_id: str, _queue_name: str | None = None, **kwargs: object
    ) -> object | None:
        self.calls.append((function, _job_id, _queue_name, kwargs))
        if _job_id in self.job_ids:
            return None
        self.job_ids.add(_job_id)
        return object()


def test_poll_job_id_is_one_per_mailbox_whatever_the_spelling() -> None:
    assert gmail_poll_job_id(" Buyer@Example.com ") == gmail_poll_job_id("buyer@example.com")
    assert gmail_poll_job_id("buyer@example.com") != gmail_poll_job_id("other@example.com")


def test_poll_job_id_keeps_the_address_out_of_redis_keys() -> None:
    job_id = gmail_poll_job_id("buyer@example.com")

    assert job_id.startswith("gmail-poll:")
    assert len(job_id) == len("gmail-poll:") + 40
    assert "buyer" not in job_id
    assert "example" not in job_id


async def test_fan_out_queues_new_polls_and_counts_pending_ones() -> None:
    redis = _FakeArq()
    redis.job_ids.add(gmail_poll_job_id("busy@example.com"))

    result = await enqueue_mailbox_polls(
        redis,  # type: ignore[arg-type]
        ["new@example.com", "busy@example.com"],
        job_name="poll_gmail_mailbox_task",
    )

    assert result == PollFanOut(enqueued=1, already_pending=1)
    assert redis.calls[0] == (
        "poll_gmail_mailbox_task",
        gmail_poll_job_id("new@example.com"),
        None,
        {"mailbox_email": "new@example.com"},
    )


async def test_the_poll_cron_queues_the_registered_poll_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.workers.tasks as tasks

    async def mailboxes() -> list[str]:
        return ["a@example.com"]

    redis = _FakeArq()
    monkeypatch.setattr(tasks, "list_polled_mailboxes", mailboxes)

    await tasks.poll_gmail_mailboxes_task({"redis": redis})

    assert [(call[0], call[3]) for call in redis.calls] == [
        (tasks.poll_gmail_mailbox_task.__name__, {"mailbox_email": "a@example.com"})
    ]


async def test_a_poll_with_no_connected_mailboxes_queues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.workers.tasks as tasks

    async def no_mailboxes() -> list[str]:
        return []

    redis = _FakeArq()
    monkeypatch.setattr(tasks, "list_polled_mailboxes", no_mailboxes)

    await tasks.poll_gmail_mailboxes_task({"redis": redis})

    assert redis.calls == []


async def test_a_poll_syncs_from_the_cursor_and_skips_a_busy_mailbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.workers.tasks as tasks

    calls: list[dict[str, object]] = []

    async def sync(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(tasks, "run_gmail_sync", sync)

    await tasks.poll_gmail_mailbox_task({}, mailbox_email="a@example.com")

    assert calls == [
        {
            "mailbox_email": "a@example.com",
            "notification_history_id": None,
            "force_sync": True,
            "skip_if_busy": True,
        }
    ]
