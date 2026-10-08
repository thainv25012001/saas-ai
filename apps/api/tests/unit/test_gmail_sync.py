from __future__ import annotations

from typing import Any

import pytest

from app.gmail.client import GmailAPIError
from app.gmail.sync import (
    GmailAttachment,
    extract_xlsx_attachments,
    fetch_attachment_if_available,
    fetch_history_batch,
    fetch_message_if_available,
    notification_is_behind_cursor,
)

pytestmark = pytest.mark.anyio


class _HistoryClient:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, str | None]] = []

    async def list_history(
        self,
        _token: str,
        start_id: str,
        *,
        page_token: str | None = None,
        max_results: int = 500,
    ) -> dict[str, Any]:
        self.calls.append((start_id, page_token))
        if max_results != 500:
            raise AssertionError("history page size should be bounded to Gmail's maximum")
        return self.pages[len(self.calls) - 1]


class _MissingMessageClient:
    async def get_message(self, _token: str, _message_id: str) -> dict[str, object]:
        raise GmailAPIError("message not found", status_code=404)


class _MissingAttachmentClient:
    async def get_attachment(self, _token: str, _message_id: str, _attachment_id: str) -> bytes:
        raise GmailAPIError("attachment not found", status_code=404)


async def test_history_pagination_deduplicates_message_ids_and_tracks_latest_cursor() -> None:
    client = _HistoryClient(
        [
            {
                "history": [
                    {"id": "21", "messagesAdded": [{"message": {"id": "a"}}]},
                    {"id": "22", "messagesAdded": [{"message": {"id": "b"}}]},
                ],
                "historyId": "25",
                "nextPageToken": "page-2",
            },
            {
                "history": [
                    {"id": "23", "messagesAdded": [{"message": {"id": "a"}}]},
                    {"id": "24", "messagesAdded": [{"message": {"id": "c"}}]},
                ],
                "historyId": "26",
            },
        ]
    )

    batch = await fetch_history_batch(client, "access", "20")

    assert batch.message_ids == ["a", "b", "c"]
    assert batch.history_id == "26"
    assert client.calls == [("20", None), ("20", "page-2")]


def test_notification_ordering_uses_numeric_history_ids() -> None:
    assert notification_is_behind_cursor("90", "100")
    assert not notification_is_behind_cursor("100", "100")
    assert not notification_is_behind_cursor("101", "100")


def test_extracts_multiple_nested_xlsx_attachments_and_ignores_other_files() -> None:
    message = {
        "payload": {
            "parts": [
                {
                    "filename": "quote-one.xlsx",
                    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "body": {"attachmentId": "attachment-one", "size": 12},
                },
                {
                    "mimeType": "multipart/related",
                    "parts": [
                        {
                            "filename": "notes.pdf",
                            "mimeType": "application/pdf",
                            "body": {"attachmentId": "pdf-attachment"},
                        },
                        {
                            "filename": "quote-two.XLSX",
                            "mimeType": "application/octet-stream",
                            "body": {"attachmentId": "attachment-two", "size": 24},
                        },
                    ],
                },
            ]
        }
    }

    attachments = extract_xlsx_attachments(message)

    assert [item.attachment_id for item in attachments] == ["attachment-one", "attachment-two"]
    assert [item.filename for item in attachments] == ["quote-one.xlsx", "quote-two.XLSX"]


async def test_message_deleted_after_history_fetch_is_skipped_without_retry() -> None:
    assert await fetch_message_if_available(_MissingMessageClient(), "token", "gone") is None


async def test_missing_attachment_is_handled_as_a_reviewable_race() -> None:
    attachment = GmailAttachment(attachment_id="gone", filename="quote.xlsx")

    assert (
        await fetch_attachment_if_available(
            _MissingAttachmentClient(), "token", "message", attachment
        )
        is None
    )
