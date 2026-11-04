"""Thin async client for the mautrix-meta provisioning API (`/_matrix/provision/v3`).

The bot calls it with its own Matrix access token and `?user_id=<bot>`: the bridge checks the
token against the homeserver (`provisioning.allow_matrix_auth`) and acts on that user's own
Facebook login, so no shared provisioning secret is needed. The token never appears in a repr,
an error or a log line.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

REDACTED = "<redacted>"


@dataclass(frozen=True)
class BridgeUser:
    """A Facebook user as the bridge resolves it: numeric id, name, ghost and DM portal."""

    id: str
    name: str | None
    mxid: str | None
    dm_room_mxid: str | None


class BridgeError(Exception):
    """A failed provisioning call: HTTP status (None for a transport error), errcode, text."""

    def __init__(self, status: int | None, errcode: str | None, error: str):
        super().__init__(status, errcode, error)
        self.status = status
        self.errcode = errcode
        self.error = error

    def __str__(self) -> str:
        return f"{self.status} {self.errcode}: {self.error}"


class BridgeUnavailable(BridgeError):
    """Transport error or 5xx: retry later."""


def _user(body: Any) -> BridgeUser:
    if not isinstance(body, dict) or not isinstance(body.get("id"), str):
        raise BridgeError(None, None, "unexpected provisioning answer")

    def text(key: str) -> str | None:
        value = body.get(key)
        return value if isinstance(value, str) and value else None

    return BridgeUser(body["id"], text("name"), text("mxid"), text("dm_room_mxid"))


class ProvisioningClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        user_id: str,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._user_id = user_id
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"ProvisioningClient(base_url={self._base_url!r}, user_id={self._user_id!r})"

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

    async def whoami(self) -> dict:
        """The bridge's view of the user: logins, `management_room`, `command_prefix`."""
        body = await self._request("GET", "/v3/whoami")
        if not isinstance(body, dict):
            raise BridgeError(None, None, "unexpected provisioning answer")
        return body

    async def search_users(self, query: str) -> list[BridgeUser]:
        """People the login can message whose name matches `query`."""
        body = await self._request("POST", "/v3/search_users", json={"query": query})
        results = body.get("results") if isinstance(body, dict) else None
        return [_user(item) for item in results or []]

    async def create_dm(self, facebook_id: str) -> BridgeUser:
        """Open (or find) the DM portal with the Facebook user; `dm_room_mxid` is its room."""
        return _user(await self._request("POST", f"/v3/create_dm/{quote(facebook_id, safe='')}"))

    def _redact(self, text: str) -> str:
        return text.replace(self._token, REDACTED) if self._token else text

    async def _request(self, method: str, path: str, *, json: Any = None) -> Any:
        try:
            response = await self._http.request(
                method, path, params={"user_id": self._user_id}, json=json
            )
        except httpx.TransportError as exc:
            raise BridgeUnavailable(
                None, None, self._redact(f"{type(exc).__name__}: {exc}")
            ) from None
        if response.is_success:
            try:
                return response.json()
            except ValueError:
                raise BridgeError(response.status_code, None, "response is not JSON") from None
        errcode, error = None, response.reason_phrase
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            errcode = body.get("errcode") if isinstance(body.get("errcode"), str) else None
            error = str(body.get("error") or error)
        error = self._redact(error)
        if response.status_code >= 500:
            raise BridgeUnavailable(response.status_code, errcode, error)
        raise BridgeError(response.status_code, errcode, error)
