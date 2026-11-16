"""The connector's record of approval messages and digests it posted.

The bot is the only writer. Writers take a connection and never commit, so the caller owns the
transaction; every state change is a status-guarded update.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import psycopg


@dataclass(frozen=True)
class Target:
    kind: Literal["approval", "digest"]
    decision_ids: tuple[int, ...]


async def posted_ids(conn: psycopg.AsyncConnection) -> set[int]:
    cur = await conn.execute("SELECT decision_id FROM approval_messages")
    return {row[0] for row in await cur.fetchall()}


async def record_message(
    conn: psycopg.AsyncConnection, decision_id: int, room_id: str, event_id: str
) -> None:
    await conn.execute(
        "INSERT INTO approval_messages (decision_id, room_id, event_id, status)"
        " VALUES (%s, %s, %s, 'posted') ON CONFLICT (decision_id) DO NOTHING",
        (decision_id, room_id, event_id),
    )


async def record_digest(
    conn: psycopg.AsyncConnection, room_id: str, event_id: str, decision_ids: list[int]
) -> int:
    cur = await conn.execute(
        "INSERT INTO approval_digests (room_id, event_id, decision_ids) VALUES (%s, %s, %s)"
        " ON CONFLICT (event_id) DO UPDATE SET event_id = EXCLUDED.event_id RETURNING id",
        (room_id, event_id, decision_ids),
    )
    row = await cur.fetchone()
    assert row is not None
    await conn.execute(
        "UPDATE approval_messages SET digest_id = %s, updated_at = now()"
        " WHERE decision_id = ANY(%s) AND digest_id IS NULL",
        (row[0], decision_ids),
    )
    return row[0]


async def close_missing(conn: psycopg.AsyncConnection, open_ids: set[int]) -> int:
    cur = await conn.execute(
        "UPDATE approval_messages SET status = 'closed', updated_at = now()"
        " WHERE status = 'posted' AND NOT (decision_id = ANY(%s::bigint[]))",
        (sorted(open_ids),),
    )
    return cur.rowcount


async def find_target(conn: psycopg.AsyncConnection, event_id: str) -> Target | None:
    cur = await conn.execute(
        "SELECT decision_id FROM approval_messages WHERE event_id = %s", (event_id,)
    )
    row = await cur.fetchone()
    if row is not None:
        return Target("approval", (row[0],))
    cur = await conn.execute(
        "SELECT decision_ids FROM approval_digests WHERE event_id = %s", (event_id,)
    )
    row = await cur.fetchone()
    if row is not None:
        return Target("digest", tuple(row[0]))
    return None
