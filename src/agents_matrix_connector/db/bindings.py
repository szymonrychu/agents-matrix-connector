"""The hard 1:1 room-session binding (writer: bot worker; the api only reads).

A binding is active while `released_at` is NULL. Two partial unique indexes make a second active
binding of one room, or of one session, impossible; nothing else binds (no `runs` or `sends`
row is read as a binding). Writers take a connection and never commit, so the caller owns the
transaction; a release is one guarded UPDATE on `released_at IS NULL`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import psycopg
from psycopg.rows import dict_row

_COLUMNS = (
    "id, room_id, session_id, slug, origin, run_id, send_id, created_at, released_at,"
    " release_reason"
)
_REFUSALS: dict[str, Literal["room_bound", "session_bound"]] = {
    "room_bindings_active_room_idx": "room_bound",
    "room_bindings_active_session_idx": "session_bound",
}

BindOutcome = Literal["bound", "already", "room_bound", "session_bound"]


@dataclass(frozen=True)
class BindingRow:
    id: int
    room_id: str
    session_id: uuid.UUID | None
    slug: str | None
    origin: str
    run_id: int | None
    send_id: int | None
    created_at: datetime
    released_at: datetime | None
    release_reason: str | None


def refusal_for_index(name: str | None) -> Literal["room_bound", "session_bound"] | None:
    """The refusal a unique violation of the named index means; None for any other index."""
    return _REFUSALS.get(name) if name else None


async def _one(conn: psycopg.AsyncConnection, where: str, params: tuple) -> BindingRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM room_bindings WHERE released_at IS NULL AND {where}", params
        )
        row = await cur.fetchone()
    return BindingRow(**row) if row else None


async def active_for_room(conn: psycopg.AsyncConnection, room_id: str) -> BindingRow | None:
    """The room's active binding, a reservation (no session yet) included."""
    return await _one(conn, "room_id = %s", (room_id,))


async def active_for_session(
    conn: psycopg.AsyncConnection, session_id: uuid.UUID
) -> BindingRow | None:
    return await _one(conn, "session_id = %s", (session_id,))


async def active_with_session(conn: psycopg.AsyncConnection) -> list[BindingRow]:
    """Every active binding that has a session, oldest first (the binding sweep)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM room_bindings"
            " WHERE released_at IS NULL AND session_id IS NOT NULL ORDER BY id"
        )
        return [BindingRow(**row) for row in await cur.fetchall()]


async def reserve_for_run(conn: psycopg.AsyncConnection, room_id: str, run_id: int) -> bool:
    """Reserve an unbound room for a first run (no session yet); False when it is bound."""
    cur = await conn.execute(
        """
        INSERT INTO room_bindings (room_id, origin, run_id) VALUES (%s, 'inbound', %s)
        ON CONFLICT (room_id) WHERE released_at IS NULL DO NOTHING
        """,
        (room_id, run_id),
    )
    return cur.rowcount == 1


async def fill_session(
    conn: psycopg.AsyncConnection, run_id: int, session_id: uuid.UUID | str, slug: str
) -> bool:
    """Fill the run's active reservation with its session; True again for the same session."""
    cur = await conn.execute(
        """
        UPDATE room_bindings SET session_id = %(session)s, slug = %(slug)s
        WHERE run_id = %(run)s AND released_at IS NULL
          AND (session_id IS NULL OR session_id = %(session)s)
        """,
        {"session": session_id, "slug": slug, "run": run_id},
    )
    return cur.rowcount == 1


async def bind_for_send(
    conn: psycopg.AsyncConnection,
    *,
    room_id: str,
    session_id: uuid.UUID,
    slug: str | None,
    send_id: int,
) -> BindOutcome:
    """Bind an unbound room to a session bound to no room, for the send that initiates there.

    `already` when the room is the session's own; `room_bound` when it is bound to or reserved
    by another session; `session_bound` when the session is bound elsewhere. A binding that
    appears concurrently loses on the unique indexes inside a savepoint and maps to the same
    refusals, so the caller's transaction stays usable.
    """
    room = await active_for_room(conn, room_id)
    if room is not None:
        return "already" if room.session_id == session_id else "room_bound"
    if await active_for_session(conn, session_id) is not None:
        return "session_bound"
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO room_bindings (room_id, session_id, slug, origin, send_id)"
                " VALUES (%s, %s, %s, 'agent', %s)",
                (room_id, session_id, slug, send_id),
            )
    except psycopg.errors.UniqueViolation as exc:
        refusal = refusal_for_index(exc.diag.constraint_name)
        if refusal is None:
            raise
        return refusal
    return "bound"


async def release(
    conn: psycopg.AsyncConnection,
    binding_id: int,
    reason: str,
    *,
    session_id: uuid.UUID | None = None,
) -> bool:
    """Release an active binding, guarded on the expected session when one is given."""
    cur = await conn.execute(
        """
        UPDATE room_bindings SET released_at = now(), release_reason = %(reason)s
        WHERE id = %(id)s AND released_at IS NULL
          AND (%(session)s::uuid IS NULL OR session_id = %(session)s::uuid)
        """,
        {"id": binding_id, "reason": reason, "session": session_id},
    )
    return cur.rowcount == 1


async def release_for_send(conn: psycopg.AsyncConnection, send_id: int, reason: str) -> bool:
    """Release the active binding the send created by initiating; False when it has none."""
    cur = await conn.execute(
        """
        UPDATE room_bindings SET released_at = now(), release_reason = %s
        WHERE send_id = %s AND origin = 'agent' AND released_at IS NULL
        """,
        (reason, send_id),
    )
    return cur.rowcount == 1


async def prune_released(conn: psycopg.AsyncConnection, older_than: datetime) -> int:
    cur = await conn.execute("DELETE FROM room_bindings WHERE released_at < %s", (older_than,))
    return cur.rowcount
