"""Approvals: the taskboard delegate client, message texts and verdict parsers."""

from collections.abc import Sequence
from typing import Any

import httpx

GO, NO_GO = "go", "no_go"

BASE = "/taskboard/api/v1/approvals"
_IGNORED_MODIFIERS = {"\N{VARIATION SELECTOR-16}"} | {chr(c) for c in range(0x1F3FB, 0x1F400)}
_REACTIONS = {
    "\N{THUMBS UP SIGN}": GO,
    "\N{WHITE HEAVY CHECK MARK}": GO,
    "\N{THUMBS DOWN SIGN}": NO_GO,
    "\N{CROSS MARK}": NO_GO,
}
_TEXT_GO = {"go", "yes", "approve"}
_TEXT_NO_GO = {"no-go", "no go", "nogo", "no", "deny"}
_DIGEST_GO = {"all go", "go all"}


class TaskboardError(Exception):
    """A failed taskboard call; `status` is None when no response arrived."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status >= 500


class TaskboardDelegate:
    """Client of the taskboard delegate listener (open approvals and batch answers)."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def open_approvals(self) -> list[dict]:
        return await self._items("GET", BASE, params={"status": "open", "limit": 200})

    async def answer(self, items: list[dict]) -> list[dict]:
        return await self._items(
            "POST", f"{BASE}/answer", json={"items": items, "source": "matrix"}
        )

    async def _items(self, method: str, path: str, **kwargs: Any) -> list[dict]:
        """The `items` list of a taskboard answer; anything but a list of objects is refused."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TaskboardError(None, f"taskboard unreachable ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise TaskboardError(response.status_code, f"taskboard answered {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise TaskboardError(response.status_code, "taskboard answered non-JSON") from None
        items = body.get("items", []) if isinstance(body, dict) else []
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise TaskboardError(response.status_code, "taskboard answered malformed items")
        return items


def approval_text(item: dict) -> str:
    asker = item.get("asking_slug") or "a session"
    text = f"Approval D-{item['id']} from {asker} ({item['task_key']}): {item['question']}"
    if item.get("subject"):
        text += f" Subject: {item['subject']}."
    return (
        text + " React with \N{THUMBS UP SIGN} for Go or \N{THUMBS DOWN SIGN} for No-go,"
        " or reply to this message with go or no-go."
    )


def digest_text(ids: Sequence[int]) -> str:
    listed = ", ".join(f"D-{i}" for i in ids)
    return (
        f"Open approvals {listed}. Reply to this message with all go to answer Go to exactly these."
    )


def reaction_verdict(key: str) -> str | None:
    return _REACTIONS.get("".join(c for c in key if c not in _IGNORED_MODIFIERS))


def _normalise(body: str) -> str:
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith(">")]
    return " ".join(" ".join(lines).split()).casefold().rstrip(" .!")


def text_verdict(body: str) -> str | None:
    text = _normalise(body)
    if text in _TEXT_GO:
        return GO
    if text in _TEXT_NO_GO:
        return NO_GO
    return None


def digest_verdict(body: str) -> str | None:
    return GO if _normalise(body) in _DIGEST_GO else None
