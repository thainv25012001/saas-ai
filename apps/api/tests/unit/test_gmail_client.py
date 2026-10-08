from __future__ import annotations

import base64

import httpx
import pytest

from app.gmail.client import GmailAPIError, GmailClient

pytestmark = pytest.mark.anyio


async def test_profile_watch_history_full_message_and_attachment_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/profile"):
            return httpx.Response(
                200, json={"emailAddress": "Quotes@Example.com", "historyId": "42"}
            )
        if request.url.path.endswith("/watch"):
            return httpx.Response(200, json={"historyId": "43", "expiration": "1900000000000"})
        if request.url.path.endswith("/history"):
            return httpx.Response(200, json={"history": [{"id": "44"}], "historyId": "44"})
        if request.url.path.endswith("/messages/message-1"):
            return httpx.Response(200, json={"id": "message-1", "payload": {"parts": []}})
        if request.url.path.endswith("/attachments/attachment-1"):
            return httpx.Response(
                200,
                json={
                    "data": base64.urlsafe_b64encode(b"xlsx-bytes").decode().rstrip("="),
                    "size": 10,
                },
            )
        if request.url.path.endswith("/stop"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request path: {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com"
    ) as http_client:
        gmail = GmailClient(http_client=http_client)
        profile = await gmail.get_profile("access-secret")
        watch = await gmail.watch("access-secret", "projects/p/topics/gmail")
        history = await gmail.list_history("access-secret", "42", page_token="page-1")
        message = await gmail.get_message("access-secret", "message-1")
        attachment = await gmail.get_attachment("access-secret", "message-1", "attachment-1")
        await gmail.stop_watch("access-secret")

    assert profile.email_address == "quotes@example.com"
    assert watch.history_id == "43"
    assert history["history"][0]["id"] == "44"
    assert message["id"] == "message-1"
    assert attachment == b"xlsx-bytes"
    assert all(request.headers["authorization"] == "Bearer access-secret" for request in requests)
    history_request = next(request for request in requests if request.url.path.endswith("/history"))
    assert history_request.url.params["startHistoryId"] == "42"
    assert history_request.url.params["pageToken"] == "page-1"


async def test_gmail_errors_do_not_echo_token_or_provider_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "secret email body and access token"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com"
    ) as http_client:
        gmail = GmailClient(http_client=http_client)
        with pytest.raises(GmailAPIError) as error:
            await gmail.get_message("very-secret-access-token", "message-1")

    assert "very-secret-access-token" not in str(error.value)
    assert "secret email body" not in str(error.value)


async def test_attachment_limit_is_checked_after_base64url_decode() -> None:
    data = base64.urlsafe_b64encode(b"too-large").decode().rstrip("=")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": data, "size": 9})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://gmail.googleapis.com"
    ) as http_client:
        gmail = GmailClient(http_client=http_client, max_attachment_bytes=4)
        with pytest.raises(GmailAPIError, match="size limit"):
            await gmail.get_attachment("access", "message-1", "attachment-1")
