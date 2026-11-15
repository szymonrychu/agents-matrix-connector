"""The bot's history crawler: a one-time crawl of each bound room's earlier messages.

Every room with an active binding gets one crawl, ever. A crawl pages the room's timeline
backwards with `/messages`, one page of at most `CRAWL_PAGE` events per tick (the pacing), up to
`MATRIX_HISTORY_CRAWL_MAX_EVENTS` events, and stores the messages as history rows: always
`ignored`, never dispatched to an agent.

The crawl never starts at the live edge of the timeline. It starts just before the room's
oldest stored sync row (its `/context` start token) or, with no such row, at the sync loop's
committed `next_batch`: everything older than that token was already offered to sync, so a live
event can never be stored as a history row instead of being dispatched. A page and the crawl's
progress commit together; a crash replays the page and the inserts skip what is stored. Logs
carry room ids, counts and fixed reasons only, never bodies, names, urls or tokens.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import discovery
from agents_matrix_connector.clock import utcnow
from agents_matrix_connector.db import directory, history_crawls, inbound, rules
from agents_matrix_connector.db.history_crawls import HistoryCrawl
from agents_matrix_connector.matrix import (
    MAX_RETRY_SECONDS,
    MatrixAuthError,
    MatrixClient,
    MatrixError,
    MatrixForbidden,
    MatrixNotFound,
    MatrixUnavailable,
)
from agents_matrix_connector.settings import BotSettings
from agents_matrix_connector.sync import SyncLoop

log = logging.getLogger(__name__)

CRAWL_PAGE = 100
CRAWL_FILTER = {"types": [*discovery.MESSAGE_TYPES, "m.room.redaction"]}
COMPLETE = "complete"
CAP = "cap"
NOT_VISIBLE = "not_visible"
MATRIX_ERROR = "matrix_error"
DEFAULT_RETRY_SECONDS = 60


class HistoryCrawler:
    def __init__(
        self,
        matrix: MatrixClient,
        pool: AsyncConnectionPool,
        sync: SyncLoop,
        settings: BotSettings,
        *,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._matrix = matrix
        self._pool = pool
        self._sync = sync
        self._cap = settings.history_crawl_max_events
        self._now = now

    async def tick(self) -> None:
        """At most one Matrix page for the oldest ready crawl. A MatrixAuthError propagates."""
        if self._cap == 0:
            return
        now = self._now()
        async with self._pool.connection() as conn, conn.transaction():
            queued = await history_crawls.enqueue_bound(conn)
            crawl = await history_crawls.next_ready(conn, now)
        if queued:
            log.info("history crawls queued: %d", queued)
        if crawl is None:
            return
        status = crawl.status
        try:
            token = crawl.token if status == "crawling" else None
            if token is None:
                token = await self._start_token(crawl.room_id)
                if token is None:
                    log.info("room %s: no sync position yet, history crawl waits", crawl.room_id)
                    return
            if status == "queued":
                async with self._pool.connection() as conn, conn.transaction():
                    if not await history_crawls.start(conn, crawl.id, token):
                        return
                status = "crawling"
            await self._page(crawl, token)
        except MatrixAuthError:
            raise
        except MatrixUnavailable as exc:
            wait = DEFAULT_RETRY_SECONDS
            if exc.retry_after is not None:
                wait = min(exc.retry_after, MAX_RETRY_SECONDS)
            async with self._pool.connection() as conn, conn.transaction():
                await history_crawls.defer(conn, crawl.id, status, now + timedelta(seconds=wait))
            log.warning(
                "room %s: history crawl deferred %.0fs, matrix unavailable", crawl.room_id, wait
            )
        except MatrixForbidden:
            await self._finish(crawl, "done", NOT_VISIBLE)
        except MatrixError:
            await self._finish(crawl, "failed", MATRIX_ERROR)

    async def _start_token(self, room_id: str) -> str | None:
        """Just before the oldest stored sync row, else the committed sync position; None
        when the sync loop has no position yet (never the live edge of the timeline)."""
        async with self._pool.connection() as conn:
            oldest = await inbound.oldest_sync_event_id(conn, room_id)
            next_batch, _ = await directory.get_sync_state(conn)
        if oldest is not None:
            try:
                context = await self._matrix.event_context(room_id, oldest, limit=0)
            except (MatrixNotFound, MatrixForbidden):
                context = {}
            start = context.get("start") if isinstance(context, dict) else None
            if isinstance(start, str):
                return start
        return next_batch

    async def _page(self, crawl: HistoryCrawl, token: str) -> None:
        page = await self._matrix.room_messages(
            crawl.room_id,
            token,
            direction="b",
            limit=min(CRAWL_PAGE, self._cap - crawl.events),
            filter=CRAWL_FILTER,
        )
        chunk = page.get("chunk")
        events = [e for e in chunk if isinstance(e, dict)] if isinstance(chunk, list) else []
        end = page.get("end")
        # An empty page still counts as one event, so a crawl always ends.
        total = crawl.events + max(len(events), 1)
        reason = COMPLETE if not isinstance(end, str) else CAP if total >= self._cap else None
        async with self._pool.connection() as conn, conn.transaction():
            rule_list = await rules.list_rules(conn)
            inserted = await self._sync.intake_history(
                conn, crawl.room_id, list(reversed(events)), rule_list
            )
            await history_crawls.progress(
                conn,
                crawl.id,
                token=end if isinstance(end, str) else token,
                events=total,
                inserted=crawl.inserted + inserted,
            )
            if reason is not None:
                await history_crawls.finish(conn, crawl.id, "done", reason)
        log.info(
            "room %s: history page of %d events, %d stored", crawl.room_id, len(events), inserted
        )
        if reason is not None:
            log.info("room %s: history crawl done (%s)", crawl.room_id, reason)

    async def _finish(self, crawl: HistoryCrawl, status: str, reason: str) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            await history_crawls.finish(conn, crawl.id, status, reason)
        log.info("room %s: history crawl %s (%s)", crawl.room_id, status, reason)
