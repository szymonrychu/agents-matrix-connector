"""Per-room notes (writer: api; the bot worker only reads them for first prompts).

One row per room with an optimistic version: `put` with `expected_version == 0` creates, any
other value is one status-guarded UPDATE on the version. The caller owns the transaction. Bodies
are readable data, but callers still never log them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

_COLUMNS = "room_id, body, version, updated_at, updated_by_slug"


@dataclass(frozen=True)
class NotesRow:
    room_id: str
    body: str
    version: int
    updated_at: datetime
    updated_by_slug: str | None


async def get(conn: psycopg.AsyncConnection, room_id: str) -> NotesRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM room_notes WHERE room_id = %s", (room_id,))
        row = await cur.fetchone()
    return NotesRow(**row) if row else None


async def put(
    conn: psycopg.AsyncConnection,
    room_id: str,
    body: str,
    expected_version: int,
    slug: str | None,
) -> NotesRow | None:
    """Write the body; None means the stored version is not `expected_version`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        if expected_version == 0:
            await cur.execute(
                f"INSERT INTO room_notes (room_id, body, version, updated_by_slug)"
                f" VALUES (%s, %s, 1, %s) ON CONFLICT (room_id) DO NOTHING RETURNING {_COLUMNS}",
                (room_id, body, slug),
            )
        else:
            await cur.execute(
                f"UPDATE room_notes SET body = %s, version = version + 1, updated_at = now(),"
                f" updated_by_slug = %s WHERE room_id = %s AND version = %s"
                f" RETURNING {_COLUMNS}",
                (body, slug, room_id, expected_version),
            )
        row = await cur.fetchone()
    return NotesRow(**row) if row else None
