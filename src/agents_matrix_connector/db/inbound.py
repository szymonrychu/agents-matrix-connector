"""The inbound message queue.

The bot's sync loop inserts rows; the dispatch worker moves them. Rows change status only
through status-guarded UPDATEs (`... WHERE status = <expected>`), and writers take a
connection and never commit, so the caller owns the transaction. The API reads through the
pool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector.discovery import Media

_COLUMNS = (
    "id, event_id, room_id, sender, sender_name, body, mentioned, origin_ts, received_at,"
    " status, reason, run_id, rule_id, media_kind, media_url, media_mime, media_size,"
    " media_name, media_w, media_h, media_status, media_reason"
)
MEDIA_STATUSES = ("attached", "not_attached")


@dataclass(frozen=True)
class InboundRow:
    id: int
    event_id: str
    room_id: str
    sender: str
    sender_name: str | None
    body: str | None
    mentioned: bool
    origin_ts: datetime | None
    received_at: datetime
    status: str
    reason: str | None
    run_id: int | None
    rule_id: int | None = None
    media_kind: str | None = None
    media_url: str | None = None
    media_mime: str | None = None
    media_size: int | None = None
    media_name: str | None = None
    media_w: int | None = None
    media_h: int | None = None
    media_status: str | None = None
    media_reason: str | None = None


@dataclass(frozen=True)
class DeniedSummary:
    """A room's messages refused by the access policy: how many, the newest, and who sent them
    as `(user_id, newest sender_name)` ordered by user id. Bodies are never read."""

    count: int
    last_at: datetime
    senders: list[tuple[str, str | None]] = field(default_factory=list)


_DENIED = "status = 'ignored' AND (reason = 'not_allowed' OR reason LIKE 'denied:%%')"


async def denied_summary(
    conn: psycopg.AsyncConnection, room_ids: list[str] | None = None
) -> dict[str, DeniedSummary]:
    """Per room, the inbound rows the policy refused (`not_allowed` or `denied: <kind>`)."""
    params = {"rooms": room_ids}
    where = f"{_DENIED} AND (%(rooms)s::text[] IS NULL OR room_id = ANY(%(rooms)s))"
    cur = await conn.execute(
        f"SELECT room_id, count(*), max(received_at) FROM inbound WHERE {where} GROUP BY room_id",
        params,
    )
    totals = {room_id: (count, last_at) for room_id, count, last_at in await cur.fetchall()}
    cur = await conn.execute(
        f"SELECT DISTINCT ON (room_id, sender) room_id, sender, sender_name FROM inbound"
        f" WHERE {where} ORDER BY room_id, sender, id DESC",
        params,
    )
    senders: dict[str, list[tuple[str, str | None]]] = {}
    for room_id, sender, sender_name in await cur.fetchall():
        senders.setdefault(room_id, []).append((sender, sender_name))
    return {
        room_id: DeniedSummary(count, last_at, senders.get(room_id, []))
        for room_id, (count, last_at) in totals.items()
    }


async def insert(
    conn: psycopg.AsyncConnection,
    *,
    event_id: str,
    room_id: str,
    sender: str,
    sender_name: str | None,
    body: str | None,
    mentioned: bool,
    origin_ts: datetime | None,
    status: str,
    reason: str | None,
    rule_id: int | None = None,
    media: Media | None = None,
) -> bool:
    """Insert one row; `media` is the declared attachment metadata (the caller passes None
    where no body is stored, and a `Media` without name in anonymized rooms)."""
    if status not in ("new", "ignored"):
        raise ValueError(f"insert status must be new or ignored, got {status!r}")
    cur = await conn.execute(
        """
        INSERT INTO inbound (event_id, room_id, sender, sender_name, body, mentioned, origin_ts,
                             status, reason, rule_id, media_kind, media_url, media_mime,
                             media_size, media_name, media_w, media_h)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (event_id) DO NOTHING
        """,
        (
            event_id,
            room_id,
            sender,
            sender_name,
            body,
            mentioned,
            origin_ts,
            status,
            reason,
            rule_id,
            media.kind if media else None,
            media.url if media else None,
            media.mime if media else None,
            media.size if media else None,
            media.name if media else None,
            media.w if media else None,
            media.h if media else None,
        ),
    )
    return cur.rowcount == 1


_AT = "COALESCE(origin_ts, received_at)"


async def promote_media(
    conn: psycopg.AsyncConnection,
    room_id: str,
    sender: str,
    at: datetime,
    window_seconds: int,
    limit: int,
) -> int:
    """Accept the sender's media rows ignored `not_mentioned` within `window_seconds` around
    an accepted mention at `at`: newest first, at most `limit` rows (the room's queue room
    left). Status-guarded: only `ignored`/`not_mentioned` media rows move to `new`."""
    if limit <= 0:
        return 0
    window = timedelta(seconds=window_seconds)
    cur = await conn.execute(
        f"""
        UPDATE inbound SET status = 'new', reason = NULL
        WHERE id IN (
            SELECT id FROM inbound
            WHERE room_id = %(room)s AND sender = %(sender)s
              AND status = 'ignored' AND reason = 'not_mentioned' AND media_kind IS NOT NULL
              AND {_AT} BETWEEN %(low)s AND %(high)s
            ORDER BY {_AT} DESC, id DESC
            LIMIT %(limit)s
        )
          AND status = 'ignored' AND reason = 'not_mentioned'
        """,
        {
            "room": room_id,
            "sender": sender,
            "low": at - window,
            "high": at + window,
            "limit": limit,
        },
    )
    return cur.rowcount


async def recent_accepted_mention(
    conn: psycopg.AsyncConnection, room_id: str, sender: str, at: datetime, window_seconds: int
) -> bool:
    """Whether the sender has an accepted mention in the room within `window_seconds` before
    `at`."""
    cur = await conn.execute(
        f"""
        SELECT 1 FROM inbound
        WHERE room_id = %s AND sender = %s AND mentioned AND status <> 'ignored'
          AND {_AT} BETWEEN %s AND %s
        LIMIT 1
        """,
        (room_id, sender, at - timedelta(seconds=window_seconds), at),
    )
    return await cur.fetchone() is not None


async def set_media_outcome(
    conn: psycopg.AsyncConnection, row_id: int, run_id: int, status: str, reason: str | None
) -> bool:
    """Record whether a row's media reached the agent; guarded on the row being in progress
    for `run_id`."""
    if status not in MEDIA_STATUSES:
        raise ValueError(f"media status must be attached or not_attached, got {status!r}")
    cur = await conn.execute(
        "UPDATE inbound SET media_status = %s, media_reason = %s"
        " WHERE id = %s AND run_id = %s AND status = 'in-progress'",
        (status, reason, row_id, run_id),
    )
    return cur.rowcount == 1


async def count_new(conn: psycopg.AsyncConnection, room_id: str) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM inbound WHERE room_id = %s AND status = 'new'", (room_id,)
    )
    return (await cur.fetchone())[0]


async def rooms_ready(conn: psycopg.AsyncConnection, now: datetime) -> list[str]:
    """Rooms with new rows, no run in progress and no hold running past `now`, by their oldest
    new row."""
    cur = await conn.execute(
        """
        SELECT i.room_id FROM inbound i
        WHERE i.status = 'new'
          AND NOT EXISTS (SELECT 1 FROM runs r
                           WHERE r.room_id = i.room_id AND r.status = 'in-progress')
          AND NOT EXISTS (SELECT 1 FROM room_holds h
                           WHERE h.room_id = i.room_id AND h.until > %s)
        GROUP BY i.room_id
        ORDER BY min(i.id)
        """,
        (now,),
    )
    return [row[0] for row in await cur.fetchall()]


async def claim_room(conn: psycopg.AsyncConnection, room_id: str, run_id: int) -> list[InboundRow]:
    """Move every new row of the room to in-progress for the run, oldest first."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            WITH claimed AS (
                UPDATE inbound SET status = 'in-progress', run_id = %s
                WHERE room_id = %s AND status = 'new'
                RETURNING {_COLUMNS}
            )
            SELECT * FROM claimed ORDER BY id
            """,
            (run_id, room_id),
        )
        return [InboundRow(**row) for row in await cur.fetchall()]


async def run_rows(conn: psycopg.AsyncConnection, run_id: int) -> list[InboundRow]:
    """The run's in-progress rows, oldest first."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM inbound"
            " WHERE run_id = %s AND status = 'in-progress' ORDER BY id",
            (run_id,),
        )
        return [InboundRow(**row) for row in await cur.fetchall()]


async def finish_run_rows(
    conn: psycopg.AsyncConnection, run_id: int, status: str, reason: str | None = None
) -> int:
    if status not in ("handled", "failed"):
        raise ValueError(f"finish status must be handled or failed, got {status!r}")
    cur = await conn.execute(
        "UPDATE inbound SET status = %s, reason = %s WHERE run_id = %s AND status = 'in-progress'",
        (status, reason, run_id),
    )
    return cur.rowcount


async def requeue_run_rows(conn: psycopg.AsyncConnection, run_id: int) -> int:
    cur = await conn.execute(
        "UPDATE inbound SET status = 'new', run_id = NULL"
        " WHERE run_id = %s AND status = 'in-progress'",
        (run_id,),
    )
    return cur.rowcount


async def ignore_new(conn: psycopg.AsyncConnection, room_id: str, reason: str) -> int:
    cur = await conn.execute(
        "UPDATE inbound SET status = 'ignored', reason = %s WHERE room_id = %s AND status = 'new'",
        (reason, room_id),
    )
    return cur.rowcount


async def list_messages(
    pool: AsyncConnectionPool,
    *,
    room_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> list[InboundRow]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT {_COLUMNS} FROM inbound
            WHERE (%(room)s::text IS NULL OR room_id = %(room)s)
              AND (%(status)s::text IS NULL OR status = %(status)s)
              AND (%(before)s::bigint IS NULL OR id < %(before)s)
            ORDER BY id DESC
            LIMIT %(limit)s
            """,
            {"room": room_id, "status": status, "before": before_id, "limit": limit},
        )
        return [InboundRow(**row) for row in await cur.fetchall()]


async def prune(conn: psycopg.AsyncConnection, older_than: datetime) -> int:
    cur = await conn.execute(
        "DELETE FROM inbound WHERE received_at < %s AND status IN ('handled', 'ignored', 'failed')",
        (older_than,),
    )
    return cur.rowcount
