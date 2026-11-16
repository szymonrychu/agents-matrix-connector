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
REPLY_PHRASES = {
    "already_answered": "already answered",
    "expired": "expired",
    "withdrawn": "was withdrawn",
    "not_found": "is not open",
}


def reply_text(results: list[dict]) -> str | None:
    """One short line naming every approval this answer did not answer, or None."""
    parts = [
        f"D-{r['id']} {REPLY_PHRASES.get(r.get('result'), 'is not open')}."
        for r in results
        if "id" in r and r.get("result") != "answered"
    ]
    return " ".join(parts) or None


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
        await self._answer_pending()
        await self._post_new()

    async def _answer_pending(self) -> None:
        """Send each pending answer to the taskboard; a short reply quotes the answer event
        when some of its approvals were not answered. Taskboard down: stop, retry next tick."""
        assert self._delegate is not None
        async with self._pool.connection() as conn:
            pending = await repo.claim_pending_answers(conn)
        for answer in pending:
            items = [
                {"id": d, "verdict": answer.verdict, "answer_ref": answer.event_id}
                for d in answer.decision_ids
            ]
            try:
                results = await self._delegate.answer(items)
            except TaskboardError as exc:
                if exc.retryable:
                    log.warning("approvals: taskboard unavailable (%s); answers wait", exc)
                    return
                log.warning("approvals: answer %s refused (%s)", answer.event_id, exc)
                results = [{"error": exc.status}]
            async with self._pool.connection() as conn:
                await repo.finish_answer(conn, answer.event_id, results)
            text = reply_text(results)
            if text is not None:
                await self._matrix.send_event(
                    answer.room_id,
                    "m.room.message",
                    f"approval-reply-{answer.event_id}",
                    {
                        "msgtype": "m.text",
                        "body": text,
                        "m.relates_to": {"m.in_reply_to": {"event_id": answer.event_id}},
                    },
                )

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
