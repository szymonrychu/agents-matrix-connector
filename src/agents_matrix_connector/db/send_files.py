"""The files of agent sends (`send_files`): one row per file, keyed by send and position.

The api inserts the rows with their send, bytes in `data`; the bot uploads each file once
(`content_uri`) and clears `data` once the send has settled (`clear_settled`). Writers take a
connection and never commit, so the caller owns the transaction. Rows go with their send
(`ON DELETE CASCADE`), so `sends.prune` removes them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row

_COLUMNS = "send_id, position, path, name, mime, msgtype, size, sha256, w, h, content_uri"
ROOM_WINDOW_SECONDS = 3600


@dataclass(frozen=True)
class SendFile:
    send_id: int
    position: int
    path: str
    name: str
    mime: str
    msgtype: str
    size: int
    sha256: str
    w: int | None
    h: int | None
    content_uri: str | None


@dataclass(frozen=True)
class NewFile:
    """A file the api accepted, with its bytes; `size` and `sha256` describe `data`."""

    position: int
    path: str
    name: str
    mime: str
    msgtype: str
    size: int
    sha256: str
    w: int | None
    h: int | None
    data: bytes


async def insert_many(
    conn: psycopg.AsyncConnection, send_id: int, files: Sequence[NewFile]
) -> None:
    for f in files:
        await conn.execute(
            """
            INSERT INTO send_files (send_id, position, path, name, mime, msgtype, size, sha256,
                                    w, h, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                send_id,
                f.position,
                f.path,
                f.name,
                f.mime,
                f.msgtype,
                f.size,
                f.sha256,
                f.w,
                f.h,
                f.data,
            ),
        )


async def get(
    conn: psycopg.AsyncConnection, send_id: int, position: int
) -> tuple[SendFile, bytes | None] | None:
    """The file and its bytes (None once cleared), None when there is no such file."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS}, data FROM send_files WHERE send_id = %s AND position = %s",
            (send_id, position),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    data = row.pop("data")
    return SendFile(**row), None if data is None else bytes(data)


async def set_content_uri(
    conn: psycopg.AsyncConnection, send_id: int, position: int, uri: str
) -> None:
    await conn.execute(
        "UPDATE send_files SET content_uri = %s WHERE send_id = %s AND position = %s",
        (uri, send_id, position),
    )


async def list_for_send(conn: psycopg.AsyncConnection, send_id: int) -> list[SendFile]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM send_files WHERE send_id = %s ORDER BY position",
            (send_id,),
        )
        return [SendFile(**row) for row in await cur.fetchall()]


async def delivery(conn: psycopg.AsyncConnection, send_id: int) -> dict[int, str]:
    """The status of each file's outbound row by position; a file not placed yet is absent."""
    cur = await conn.execute(
        "SELECT file_position, status FROM outbound"
        " WHERE send_id = %s AND file_position IS NOT NULL",
        (send_id,),
    )
    return {position: status for position, status in await cur.fetchall()}


async def room_bytes_last_hour(conn: psycopg.AsyncConnection, room_id: str) -> int:
    """File bytes of the room's sends created in the last hour, by any session; a refused send
    delivered nothing and does not count."""
    cur = await conn.execute(
        "SELECT COALESCE(sum(f.size), 0) FROM send_files f JOIN sends s ON s.id = f.send_id"
        " WHERE s.room_id = %s AND s.status <> 'refused'"
        " AND s.created_at > now() - make_interval(secs => %s)",
        (room_id, ROOM_WINDOW_SECONDS),
    )
    return int((await cur.fetchone())[0])


async def clear_settled(conn: psycopg.AsyncConnection) -> int:
    """Drop the bytes of every file whose send has settled (sent, failed or refused)."""
    cur = await conn.execute(
        "UPDATE send_files SET data = NULL FROM sends"
        " WHERE sends.id = send_files.send_id AND sends.status IN ('sent', 'failed', 'refused')"
        " AND send_files.data IS NOT NULL"
    )
    return cur.rowcount
