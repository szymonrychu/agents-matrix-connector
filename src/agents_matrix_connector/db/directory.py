"""Rooms, users, members and the sync state.

The bot's sync loop is the only writer: writers take a connection and never commit, so the
caller owns one transaction per sync response. The API reads through the pool.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector.discovery import RoomInfo


@dataclass(frozen=True)
class SessionRef:
    session_id: uuid.UUID | None
    slug: str | None
    status: str


@dataclass(frozen=True)
class MemberRow:
    user_id: str
    display_name: str | None
    facebook_id: str | None
    membership: str


@dataclass(frozen=True)
class RoomRow:
    room_id: str
    kind: str | None
    name: str | None
    alias: str | None
    encrypted: bool
    message_request: bool
    membership: str
    inviter: str | None
    member_count: int
    discovered_at: datetime
    last_event_at: datetime | None
    queued: int
    session: SessionRef | None
    members: list[MemberRow] = field(default_factory=list)


@dataclass(frozen=True)
class UserRow:
    user_id: str
    display_name: str | None
    facebook_id: str | None
    room_ids: list[str]


# --- writers (bot sync) -----------------------------------------------------------------------


async def upsert_room(
    conn: psycopg.AsyncConnection,
    room_id: str,
    *,
    membership: str,
    info: RoomInfo | None = None,
    inviter: str | None = None,
    at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO rooms (room_id, membership, inviter, kind, name, alias, encrypted,
                           message_request, member_count, discovered_at, updated_at)
        VALUES (%(room_id)s, %(membership)s, %(inviter)s, %(kind)s, %(name)s, %(alias)s,
                COALESCE(%(encrypted)s, false), COALESCE(%(message_request)s, false),
                COALESCE(%(member_count)s, 0), %(at)s, %(at)s)
        ON CONFLICT (room_id) DO UPDATE SET
            membership = EXCLUDED.membership,
            inviter = COALESCE(EXCLUDED.inviter, rooms.inviter),
            kind = COALESCE(EXCLUDED.kind, rooms.kind),
            name = CASE WHEN %(has_info)s THEN EXCLUDED.name ELSE rooms.name END,
            alias = CASE WHEN %(has_info)s THEN EXCLUDED.alias ELSE rooms.alias END,
            encrypted = COALESCE(%(encrypted)s, rooms.encrypted),
            message_request = COALESCE(%(message_request)s, rooms.message_request),
            member_count = COALESCE(%(member_count)s, rooms.member_count),
            updated_at = EXCLUDED.updated_at
        """,
        {
            "room_id": room_id,
            "membership": membership,
            "inviter": inviter,
            "kind": info.kind if info else None,
            "name": info.name if info else None,
            "alias": info.alias if info else None,
            "encrypted": info.encrypted if info else None,
            "message_request": info.message_request if info else None,
            "member_count": info.member_count if info else None,
            "has_info": info is not None,
            "at": at,
        },
    )


async def set_membership(
    conn: psycopg.AsyncConnection, room_id: str, membership: str, at: datetime
) -> None:
    await conn.execute(
        "UPDATE rooms SET membership = %s, updated_at = %s WHERE room_id = %s",
        (membership, at, room_id),
    )


async def upsert_members(
    conn: psycopg.AsyncConnection,
    room_id: str,
    members: Mapping[str, tuple[str | None, str]],
    facebook_id: Callable[[str], str | None],
    at: datetime,
) -> None:
    for user_id, (display_name, membership) in members.items():
        await conn.execute(
            """
            INSERT INTO users (user_id, display_name, facebook_id, first_seen_at, last_seen_at)
            VALUES (%(u)s, %(d)s, %(f)s, %(at)s, %(at)s)
            ON CONFLICT (user_id) DO UPDATE SET
                display_name = COALESCE(EXCLUDED.display_name, users.display_name),
                facebook_id = COALESCE(EXCLUDED.facebook_id, users.facebook_id),
                last_seen_at = EXCLUDED.last_seen_at
            """,
            {"u": user_id, "d": display_name, "f": facebook_id(user_id), "at": at},
        )
        await conn.execute(
            """
            INSERT INTO room_members (room_id, user_id, membership, display_name, updated_at)
            VALUES (%(r)s, %(u)s, %(m)s, %(d)s, %(at)s)
            ON CONFLICT (room_id, user_id) DO UPDATE SET
                membership = EXCLUDED.membership,
                display_name = EXCLUDED.display_name,
                updated_at = EXCLUDED.updated_at
            """,
            {"r": room_id, "u": user_id, "m": membership, "d": display_name, "at": at},
        )
    await conn.execute(
        """
        UPDATE rooms SET updated_at = %(at)s, member_count =
            (SELECT count(*) FROM room_members WHERE room_id = %(r)s AND membership = 'join')
        WHERE room_id = %(r)s
        """,
        {"r": room_id, "at": at},
    )


async def touch_room(conn: psycopg.AsyncConnection, room_id: str, at: datetime) -> None:
    await conn.execute("UPDATE rooms SET last_event_at = %s WHERE room_id = %s", (at, room_id))


async def get_sync_state(conn: psycopg.AsyncConnection) -> tuple[str | None, str | None]:
    cur = await conn.execute("SELECT next_batch, filter_id FROM sync_state WHERE id = 1")
    row = await cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


async def set_sync_state(
    conn: psycopg.AsyncConnection,
    *,
    next_batch: str | None,
    filter_id: str | None = None,
    at: datetime,
) -> None:
    await conn.execute(
        """
        UPDATE sync_state SET next_batch = %s, filter_id = COALESCE(%s, filter_id),
                              last_sync_at = %s, updated_at = %s
        WHERE id = 1
        """,
        (next_batch, filter_id, at, at),
    )


# --- readers (api) ----------------------------------------------------------------------------

_ROOM_SELECT = """
    SELECT r.room_id, r.kind, r.name, r.alias, r.encrypted, r.message_request, r.membership,
           r.inviter, r.member_count, r.discovered_at, r.last_event_at,
           (SELECT count(*) FROM inbound i
             WHERE i.room_id = r.room_id AND i.status = 'new') AS queued,
           lr.session_id, lr.slug, lr.status AS run_status
    FROM rooms r
    LEFT JOIN LATERAL (
        SELECT session_id, slug, status FROM runs
        WHERE runs.room_id = r.room_id ORDER BY id DESC LIMIT 1
    ) lr ON true
"""


def _room(row: dict, members: list[MemberRow] | None = None) -> RoomRow:
    session = (
        SessionRef(row["session_id"], row["slug"], row["run_status"])
        if row["run_status"] is not None
        else None
    )
    return RoomRow(
        room_id=row["room_id"],
        kind=row["kind"],
        name=row["name"],
        alias=row["alias"],
        encrypted=row["encrypted"],
        message_request=row["message_request"],
        membership=row["membership"],
        inviter=row["inviter"],
        member_count=row["member_count"],
        discovered_at=row["discovered_at"],
        last_event_at=row["last_event_at"],
        queued=row["queued"],
        session=session,
        members=members or [],
    )


async def list_rooms(pool: AsyncConnectionPool, kind: str | None = None) -> list[RoomRow]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            _ROOM_SELECT
            + " WHERE (%(kind)s::text IS NULL OR r.kind = %(kind)s)"
            + " ORDER BY r.last_event_at DESC NULLS LAST, r.discovered_at DESC, r.room_id",
            {"kind": kind},
        )
        return [_room(row) for row in await cur.fetchall()]


async def get_room(pool: AsyncConnectionPool, room_id: str) -> RoomRow | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_ROOM_SELECT + " WHERE r.room_id = %s", (room_id,))
        row = await cur.fetchone()
        if row is None:
            return None
        await cur.execute(
            """
            SELECT m.user_id, COALESCE(m.display_name, u.display_name) AS display_name,
                   u.facebook_id, m.membership
            FROM room_members m JOIN users u USING (user_id)
            WHERE m.room_id = %s ORDER BY m.user_id
            """,
            (room_id,),
        )
        members = [MemberRow(**m) for m in await cur.fetchall()]
    return _room(row, members)


def _like(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_users(pool: AsyncConnectionPool, q: str | None, limit: int) -> list[UserRow]:
    pattern = _like(q) if q else None
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT u.user_id, u.display_name, u.facebook_id,
                   COALESCE((SELECT array_agg(m.room_id ORDER BY m.room_id)
                               FROM room_members m
                              WHERE m.user_id = u.user_id AND m.membership = 'join'),
                            '{}') AS room_ids
            FROM users u
            WHERE %(p)s::text IS NULL OR u.user_id ILIKE %(p)s OR u.display_name ILIKE %(p)s
                  OR u.facebook_id ILIKE %(p)s
            ORDER BY u.user_id
            LIMIT %(limit)s
            """,
            {"p": pattern, "limit": limit},
        )
        return [UserRow(**row) for row in await cur.fetchall()]


async def room_exists(pool: AsyncConnectionPool, room_id: str) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM rooms WHERE room_id = %s", (room_id,))
        return await cur.fetchone() is not None


async def last_sync_at(pool: AsyncConnectionPool) -> datetime | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT last_sync_at FROM sync_state WHERE id = 1")
        row = await cur.fetchone()
        return row[0] if row else None
