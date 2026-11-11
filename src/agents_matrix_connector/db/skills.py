"""Per-room skills (writer: api; the bot worker only reads the name and description index).

One row per (room, name) with an optimistic version: `put` with `expected_version == 0`
creates, any other value is one status-guarded UPDATE on the version. `lock_room` serialises
the count check of concurrent creates. The caller owns the transaction. Bodies and descriptions
are readable data, but callers still never log them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

_COLUMNS = "room_id, name, description, body, version, created_at, updated_at, updated_by_slug"


@dataclass(frozen=True)
class SkillRow:
    room_id: str
    name: str
    description: str
    body: str
    version: int
    created_at: datetime
    updated_at: datetime
    updated_by_slug: str | None


async def list_for_room(conn: psycopg.AsyncConnection, room_id: str) -> list[SkillRow]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM room_skills WHERE room_id = %s ORDER BY name", (room_id,)
        )
        return [SkillRow(**row) for row in await cur.fetchall()]


async def get(conn: psycopg.AsyncConnection, room_id: str, name: str) -> SkillRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM room_skills WHERE room_id = %s AND name = %s", (room_id, name)
        )
        row = await cur.fetchone()
    return SkillRow(**row) if row else None


async def count(conn: psycopg.AsyncConnection, room_id: str) -> int:
    cur = await conn.execute("SELECT count(*) FROM room_skills WHERE room_id = %s", (room_id,))
    return (await cur.fetchone())[0]


async def lock_room(conn: psycopg.AsyncConnection, room_id: str) -> None:
    """Hold a transaction-scoped advisory lock on the room's skills until the transaction ends."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('room_skills:' || %s, 0))", (room_id,)
    )


async def put(
    conn: psycopg.AsyncConnection,
    room_id: str,
    name: str,
    description: str,
    body: str,
    expected_version: int,
    slug: str | None,
) -> SkillRow | None:
    """Write the skill; None means the stored version is not `expected_version`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        if expected_version == 0:
            await cur.execute(
                f"INSERT INTO room_skills (room_id, name, description, body, version,"
                f" updated_by_slug) VALUES (%s, %s, %s, %s, 1, %s)"
                f" ON CONFLICT (room_id, name) DO NOTHING RETURNING {_COLUMNS}",
                (room_id, name, description, body, slug),
            )
        else:
            await cur.execute(
                f"UPDATE room_skills SET description = %s, body = %s, version = version + 1,"
                f" updated_at = now(), updated_by_slug = %s"
                f" WHERE room_id = %s AND name = %s AND version = %s RETURNING {_COLUMNS}",
                (description, body, slug, room_id, name, expected_version),
            )
        row = await cur.fetchone()
    return SkillRow(**row) if row else None


async def delete(conn: psycopg.AsyncConnection, room_id: str, name: str) -> bool:
    cur = await conn.execute(
        "DELETE FROM room_skills WHERE room_id = %s AND name = %s", (room_id, name)
    )
    return cur.rowcount == 1
