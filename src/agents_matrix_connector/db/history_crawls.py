"""The one-time history crawl queue: one row per room that was ever bound to a session.

The bot history crawler is the only writer: it inserts `queued` rows for the active bindings
(`enqueue_bound`), moves `queued` to `crawling` (`start`), records each page (`progress`),
postpones on a rate limit (`defer`) and ends a crawl as `done` or `failed` (`finish`). Every
move is a status-guarded update; writers take a connection and never commit, so the caller
owns the transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from agents_common.db.queue import transition
from psycopg.rows import dict_row

_TABLE = "history_crawls"
_COLUMNS = (
    "id, room_id, status, reason, token, events, inserted, not_before, created_at, updated_at,"
    " done_at"
)
# Fixed reason of a crawl that ended on an unexpected (non-Matrix) error.
INTERNAL_ERROR = "internal_error"
_FINISHED = frozenset({"done", "failed"})
_ACTIVE = frozenset({"queued", "crawling"})


@dataclass(frozen=True)
class HistoryCrawl:
    id: int
    room_id: str
    status: str
    reason: str | None
    token: str | None
    events: int
    inserted: int
    not_before: datetime | None
    created_at: datetime
    updated_at: datetime
    done_at: datetime | None


async def enqueue_bound(conn: psycopg.AsyncConnection) -> int:
    """Queue a crawl for every actively bound room that never had one; the count queued."""
    cur = await conn.execute(
        "INSERT INTO history_crawls (room_id, status)"
        " SELECT DISTINCT room_id, 'queued' FROM room_bindings WHERE released_at IS NULL"
        " ON CONFLICT (room_id) DO NOTHING"
    )
    return cur.rowcount


async def next_ready(conn: psycopg.AsyncConnection, now: datetime) -> HistoryCrawl | None:
    """The oldest `queued` or `crawling` crawl whose `not_before` has passed."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM {_TABLE} WHERE status IN ('queued', 'crawling')"
            " AND (not_before IS NULL OR not_before <= %s) ORDER BY id LIMIT 1",
            (now,),
        )
        row = await cur.fetchone()
    return HistoryCrawl(**row) if row else None


async def start(conn: psycopg.AsyncConnection, crawl_id: int, token: str | None) -> bool:
    """`queued` to `crawling` from `token`."""
    return await transition(
        conn,
        _TABLE,
        crawl_id,
        "queued",
        "crawling",
        token=token,
        not_before=None,
        updated_at=datetime.now(UTC),
    )


async def progress(
    conn: psycopg.AsyncConnection, crawl_id: int, *, token: str, events: int, inserted: int
) -> bool:
    """Record a page of a `crawling` crawl: the next token and the running totals."""
    return await transition(
        conn,
        _TABLE,
        crawl_id,
        "crawling",
        "crawling",
        token=token,
        events=events,
        inserted=inserted,
        not_before=None,
        updated_at=datetime.now(UTC),
    )


async def defer(
    conn: psycopg.AsyncConnection, crawl_id: int, status: str, not_before: datetime
) -> bool:
    """Postpone a crawl still in `status` (`queued` or `crawling`) until `not_before`."""
    if status not in _ACTIVE:
        raise ValueError(f"only a queued or crawling crawl is deferred, got {status!r}")
    return await transition(
        conn,
        _TABLE,
        crawl_id,
        status,
        status,
        not_before=not_before,
        updated_at=datetime.now(UTC),
    )


async def finish(conn: psycopg.AsyncConnection, crawl_id: int, status: str, reason: str) -> bool:
    """`crawling` or `queued` to `done` or `failed`; any other target is a ValueError."""
    if status not in _FINISHED:
        raise ValueError(f"a history crawl ends as done or failed, got {status!r}")
    at = datetime.now(UTC)
    for current in ("crawling", "queued"):
        if await transition(
            conn, _TABLE, crawl_id, current, status, reason=reason, done_at=at, updated_at=at
        ):
            return True
    return False
