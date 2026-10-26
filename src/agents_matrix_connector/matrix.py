"""Thin async client for the eleven Matrix client-server calls the connector makes."""

import asyncio
import json
import logging
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

API_PREFIX = "/_matrix/client/v3"
TOKEN_ERRCODES = frozenset({"M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"})
MAX_RETRY_SECONDS = 10.0
REDACTED = "<redacted>"

_sleep = asyncio.sleep


class MatrixError(Exception):
    """A failed Matrix call: HTTP status (None for a transport error), errcode, error text."""

    def __init__(self, status: int | None, errcode: str | None, error: str):
        super().__init__(status, errcode, error)
        self.status = status
        self.errcode = errcode
        self.error = error

    def __str__(self) -> str:
        return f"{self.status} {self.errcode}: {self.error}"


class MatrixAuthError(MatrixError):
    """401, or an M_UNKNOWN_TOKEN / M_MISSING_TOKEN errcode."""


class MatrixForbidden(MatrixError):
    """403."""


class MatrixNotFound(MatrixError):
    """404."""


class MatrixUnavailable(MatrixError):
    """Transport error, 5xx, or a 429 that persisted through one retry."""


def _path(*segments: str) -> str:
    return API_PREFIX + "".join("/" + quote(segment, safe="") for segment in segments)


class MatrixClient:
    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        timeout: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url
        self._token = access_token
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"MatrixClient(base_url={self._base_url!r})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def whoami(self) -> str:
        body = await self._request("GET", _path("account", "whoami"))
        return body["user_id"]

    async def create_filter(self, user_id: str, definition: dict) -> str:
        body = await self._request("POST", _path("user", user_id, "filter"), json=definition)
        return body["filter_id"]

    async def sync(
        self,
        since: str | None,
        timeout_ms: int,
        filter_id: str | None,
        full_state: bool = False,
    ) -> dict:
        params: dict[str, str] = {"timeout": str(timeout_ms)}
        if since is not None:
            params["since"] = since
        if filter_id is not None:
            params["filter"] = filter_id
        if full_state:
            params["full_state"] = "true"
        return await self._request(
            "GET", _path("sync"), params=params, timeout=timeout_ms / 1000 + 10
        )

    async def join(self, room_id: str) -> str:
        body = await self._request("POST", _path("join", room_id), json={})
        return body["room_id"]

    async def room_state(self, room_id: str) -> list[dict]:
        return await self._request("GET", _path("rooms", room_id, "state"))

    async def room_messages(
        self,
        room_id: str,
        from_token: str,
        *,
        direction: str = "b",
        limit: int = 100,
        filter: dict | None = None,
    ) -> dict:
        """One page of a room's timeline from `from_token`: `chunk` (newest first when
        paging backwards), `start` and, while more events exist, `end`."""
        params = {"from": from_token, "dir": direction, "limit": str(limit)}
        if filter is not None:
            params["filter"] = json.dumps(filter, separators=(",", ":"))
        return await self._request("GET", _path("rooms", room_id, "messages"), params=params)

    async def resolve_alias(self, alias: str) -> str | None:
        """The room id `alias` points to in the room directory, or None when it is unknown."""
        try:
            body = await self._request("GET", _path("directory", "room", alias))
        except MatrixNotFound:
            return None
        room_id = body.get("room_id") if isinstance(body, dict) else None
        return room_id if isinstance(room_id, str) else None

    async def joined_members(self, room_id: str) -> dict[str, str | None]:
        body = await self._request("GET", _path("rooms", room_id, "joined_members"))
        return {
            user_id: (member or {}).get("display_name")
            for user_id, member in body.get("joined", {}).items()
        }

    async def send_text(self, room_id: str, txn_id: str, body: str) -> str:
        answer = await self._request(
            "PUT",
            _path("rooms", room_id, "send", "m.room.message", txn_id),
            json={"msgtype": "m.text", "body": body},
        )
        return answer["event_id"]

    async def typing(
        self, user_id: str, room_id: str, typing: bool, timeout_ms: int = 30000
    ) -> None:
        content: dict[str, Any] = {"typing": True, "timeout": timeout_ms} if typing else {}
        content.setdefault("typing", False)
        await self._request("PUT", _path("rooms", room_id, "typing", user_id), json=content)

    async def send_receipt(self, room_id: str, event_id: str) -> None:
        """Mark the room read up to `event_id` with a public `m.read` receipt."""
        await self._request("POST", _path("rooms", room_id, "receipt", "m.read", event_id), json={})

    def _redact(self, text: str) -> str:
        return text.replace(self._token, REDACTED) if self._token else text

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> Any:
        request_timeout = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
        for attempt in range(2):
            try:
                response = await self._http.request(
                    method, path, params=params, json=json, timeout=request_timeout
                )
            except httpx.TransportError as exc:
                message = self._redact(f"{type(exc).__name__}: {exc}")
                raise MatrixUnavailable(None, None, message) from None
            if response.status_code == 429 and attempt == 0:
                delay = _retry_delay(response)
                log.warning("matrix rate limited on %s %s, retrying in %.1fs", method, path, delay)
                await _sleep(delay)
                continue
            if response.is_success:
                try:
                    return response.json()
                except ValueError:
                    raise MatrixError(response.status_code, None, "response is not JSON") from None
            raise self._error(response)
        raise AssertionError("unreachable")

    def _error(self, response: httpx.Response) -> MatrixError:
        status = response.status_code
        errcode, error = _error_body(response)
        error = self._redact(error)
        if status == 401 or errcode in TOKEN_ERRCODES:
            return MatrixAuthError(status, errcode, error)
        if status == 403:
            return MatrixForbidden(status, errcode, error)
        if status == 404:
            return MatrixNotFound(status, errcode, error)
        if status == 429 or status >= 500:
            return MatrixUnavailable(status, errcode, error)
        return MatrixError(status, errcode, error)


def _error_body(response: httpx.Response) -> tuple[str | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, response.text[:500] or response.reason_phrase
    if not isinstance(body, dict):
        return None, response.text[:500]
    errcode = body.get("errcode")
    error = body.get("error") or response.reason_phrase
    return (errcode if isinstance(errcode, str) else None), str(error)


def _retry_delay(response: httpx.Response) -> float:
    """Seconds to wait before retrying a 429: `retry_after_ms` from the body, else the
    `Retry-After` header, capped at MAX_RETRY_SECONDS."""
    milliseconds: float = 0
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("retry_after_ms"), int | float):
        milliseconds = body["retry_after_ms"]
    elif response.headers.get("retry-after", "").isdigit():
        milliseconds = int(response.headers["retry-after"]) * 1000
    return min(max(milliseconds, 0) / 1000, MAX_RETRY_SECONDS)
