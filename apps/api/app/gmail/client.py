from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.core.logging import get_logger

_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"

logger = get_logger(__name__)


class GmailAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GmailProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email_address: str = Field(validation_alias="emailAddress")
    history_id: str = Field(validation_alias="historyId")


class GmailWatchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    history_id: str = Field(validation_alias="historyId")
    expiration: datetime


class GmailClient:
    """Small Gmail REST client; provider error bodies and credentials stay private."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = _API_ROOT,
        max_attachment_bytes: int | None = None,
    ) -> None:
        self.http_client = http_client
        root = base_url.rstrip("/")
        self.base_url = root if root.endswith("/gmail/v1/users/me") else f"{root}/gmail/v1/users/me"
        self.max_attachment_bytes = (
            max_attachment_bytes or get_settings().gmail_attachment_max_bytes
        )

    async def get_profile(self, access_token: str) -> GmailProfile:
        body = await self._request("GET", "profile", access_token)
        try:
            profile = GmailProfile.model_validate(body)
        except ValueError as exc:
            raise GmailAPIError("Gmail returned an invalid profile response") from exc
        profile.email_address = profile.email_address.strip().casefold()
        return profile

    async def watch(self, access_token: str, topic_name: str) -> GmailWatchResult:
        body = await self._request(
            "POST",
            "watch",
            access_token,
            json={
                "topicName": topic_name,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "include",
            },
        )
        try:
            raw_expiration = body["expiration"]
            if not isinstance(raw_expiration, (str, int)):
                raise ValueError
            expiration = datetime.fromtimestamp(int(raw_expiration) / 1000, tz=UTC)
            return GmailWatchResult(history_id=str(body["historyId"]), expiration=expiration)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise GmailAPIError("Gmail returned an invalid watch response") from exc

    async def stop_watch(self, access_token: str) -> None:
        await self._request("POST", "stop", access_token, allow_empty=True)

    async def list_history(
        self,
        access_token: str,
        start_history_id: str,
        *,
        page_token: str | None = None,
        max_results: int = 500,
    ) -> dict[str, object]:
        params: dict[str, str | int] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "labelId": "INBOX",
            "maxResults": min(max(max_results, 1), 500),
        }
        if page_token:
            params["pageToken"] = page_token
        body = await self._request("GET", "history", access_token, params=params)
        return body

    async def get_message(self, access_token: str, message_id: str) -> dict[str, object]:
        path = f"messages/{quote(message_id, safe='')}"
        return await self._request(
            "GET",
            path,
            access_token,
            params={"format": "full"},
        )

    async def list_messages(
        self,
        access_token: str,
        query: str,
        *,
        page_token: str | None = None,
        max_results: int = 500,
    ) -> dict[str, object]:
        params: dict[str, str | int] = {
            "q": query,
            "maxResults": min(max(max_results, 1), 500),
        }
        if page_token:
            params["pageToken"] = page_token
        return await self._request("GET", "messages", access_token, params=params)

    async def get_attachment(
        self,
        access_token: str,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        path = f"messages/{quote(message_id, safe='')}/attachments/{quote(attachment_id, safe='')}"
        body = await self._request("GET", path, access_token)
        encoded = body.get("data")
        reported_size = body.get("size")
        if isinstance(reported_size, int) and reported_size > self.max_attachment_bytes:
            raise GmailAPIError("Gmail attachment exceeds the configured size limit")
        if not isinstance(encoded, str):
            raise GmailAPIError("Gmail returned an invalid attachment response")
        max_encoded_size = ((self.max_attachment_bytes + 2) // 3) * 4 + 4
        if len(encoded) > max_encoded_size:
            raise GmailAPIError("Gmail attachment exceeds the configured size limit")
        try:
            decoded = base64.urlsafe_b64decode(encoded + "===")
        except (ValueError, binascii.Error) as exc:
            raise GmailAPIError("Gmail returned an invalid attachment response") from exc
        if len(decoded) > self.max_attachment_bytes:
            raise GmailAPIError("Gmail attachment exceeds the configured size limit")
        return decoded

    async def _request(
        self,
        method: str,
        path: str,
        access_token: str,
        *,
        params: dict[str, str | int] | None = None,
        json: dict[str, object] | None = None,
        allow_empty: bool = False,
    ) -> dict[str, object]:
        url = f"{self.base_url}/{path}"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            if self.http_client is not None:
                response = await self.http_client.request(
                    method, url, headers=headers, params=params, json=json
                )
            else:
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.request(
                        method, url, headers=headers, params=params, json=json
                    )
        except httpx.RequestError as exc:
            raise GmailAPIError("Gmail service request failed") from exc
        if response.is_error:
            _log_provider_error(path, response)
            raise GmailAPIError(
                f"Gmail API returned HTTP {response.status_code}",
                status_code=response.status_code,
            )
        if allow_empty and not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            if allow_empty:
                return {}
            raise GmailAPIError("Gmail returned an invalid response") from exc
        if not isinstance(body, dict):
            raise GmailAPIError("Gmail returned an invalid response")
        return body


def _log_provider_error(path: str, response: httpx.Response) -> None:
    """Log Google's reason server-side only; the raised error stays generic.

    A bare "HTTP 403" from `watch` cannot tell a missing Pub/Sub publisher
    grant from a topic in the wrong project -- Google's message can.
    """
    status = reason = message = None
    try:
        error = response.json().get("error")
        if isinstance(error, dict):
            status = error.get("status")
            message = str(error.get("message", ""))[:300] or None
            details = error.get("errors")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                reason = details[0].get("reason")
    except (ValueError, AttributeError):
        pass
    logger.warning(
        "gmail_api_error",
        endpoint=path.split("/", 1)[0],
        http_status=response.status_code,
        google_status=status,
        google_reason=reason,
        google_message=message,
    )
