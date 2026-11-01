"""The inbound message queue.

The bot's sync loop inserts rows; the dispatch worker moves them. Rows change status only
through status-guarded UPDATEs (`... WHERE status = <expected>`), and writers take a
connection and never commit, so the caller owns the transaction. The API reads through the
pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_COLUMNS = (
    "id, event_id, room_id, sender, sender_name, body, mentioned, origin_ts, received_at,"
    " status, reason, run_id, rule_id"
)


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
) -> bool:
    if status not in ("new", "ignored"):
        raise ValueError(f"insert status must be new or ignored, got {status!r}")
    cur = await conn.execute(
        """
        INSERT INTO inbound (event_id, room_id, sender, sender_name, body, mentioned, origin_ts,
                             status, reason, rule_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
        ),
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
