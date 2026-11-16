"""The bot step that posts open approvals, and a digest of them, into the approvals room.

The taskboard owns the decisions; this step only mirrors the open ones into Matrix and records
what it posted in its own tables. The message transaction id is derived from the decision id,
so a re-send after a crash returns the same event instead of a duplicate message.
"""

from __future__ import annotations

import logging

from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector.approvals import (
    TaskboardDelegate,
    TaskboardError,
    approval_text,
    digest_text,
)
from agents_matrix_connector.db import approvals as repo
from agents_matrix_connector.matrix import MatrixClient
from agents_matrix_connector.settings import BotSettings

log = logging.getLogger(__name__)

POST_LIMIT = 20


class ApprovalStep:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        matrix: MatrixClient,
        delegate: TaskboardDelegate | None,
        settings: BotSettings,
    ) -> None:
        self._pool = pool
        self._matrix = matrix
        self._delegate = delegate
        self._settings = settings

    async def tick(self) -> None:
        if self._delegate is None:
            return
        await self._post_new()

    async def _post_new(self) -> None:
        assert self._delegate is not None
        try:
            open_ = await self._delegate.open_approvals()
        except TaskboardError as exc:
            if not exc.retryable:
                raise
            log.warning("approvals: taskboard unavailable (%s); posting nothing", exc)
            return
        room = self._settings.approvals_room
        async with self._pool.connection() as conn:
            known = await repo.posted_ids(conn)
        new = sorted((i for i in open_ if i["id"] not in known), key=lambda i: i["id"])
        posted: list[int] = []
        for item in new[:POST_LIMIT]:
            event_id = await self._matrix.send_text(
                room, f"approval-{item['id']}", approval_text(item)
            )
            async with self._pool.connection() as conn:
                await repo.record_message(conn, item["id"], room, event_id)
            posted.append(item["id"])
        if len(posted) >= 2:
            event_id = await self._matrix.send_text(
                room, "approval-digest-" + "-".join(map(str, posted)), digest_text(posted)
            )
            async with self._pool.connection() as conn:
                await repo.record_digest(conn, room, event_id, posted)
        async with self._pool.connection() as conn:
            closed = await repo.close_missing(conn, {i["id"] for i in open_})
        if closed:
            log.info("approvals: %d posted approvals are no longer open", closed)
