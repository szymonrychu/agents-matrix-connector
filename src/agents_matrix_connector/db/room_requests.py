"""The room request queue (open a DM, invite a user, accept a message request).

The api inserts rows and is the only one that moves `pending_confirmation` to `confirmed` or
`rejected`, and only with the taskboard decision id the orchestrator attached. The bot moves
`resolving` to `pending_confirmation`, `candidates` or `failed`, and `confirmed` through
`working` to `done` or `failed`. Every move is a status-guarded update; writers take a
connection and never commit, so the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from agents_common.db.queue import transition
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ACTIONS = ("open", "invite", "accept")
STATUSES = (
    "resolving",
    "candidates",
    "pending_confirmation",
    "confirmed",
    "working",
    "done",
    "failed",
    "rejected",
)
_COLUMNS = (
    "id, action, target_kind, target, display_name, room_id, candidates, task_key, decision_id,"
    " requested_by, requested_slug, status, reason, created_at, updated_at, confirmed_at, done_at"
)
_TABLE = "room_requests"


@dataclass(frozen=True)
class RoomRequest:
    id: int
    action: str
    target_kind: str | None
    target: str | None
    display_name: str | None
    room_id: str | None
    candidates: list[dict[str, Any]] | None
    task_key: str | None
    decision_id: int | None
    requested_by: uuid.UUID | None
    requested_slug: str | None
    status: str
    reason: str | None
    created_at: datetime
    updated_at: datetime
    confirmed_at: datetime | None
    done_at: datetime | None


async def _move(
    conn: psycopg.AsyncConnection, request_id: int, frm: str, to: str, **fields
) -> bool:
    return await transition(conn, _TABLE, request_id, frm, to, updated_at=_now(), **fields)


def _now() -> datetime:
    return datetime.now(UTC)


async def insert(
    conn: psycopg.AsyncConnection,
    *,
    action: str,
    status: str,
    target_kind: str | None = None,
    target: str | None = None,
    display_name: str | None = None,
    room_id: str | None = None,
    candidates: list[dict[str, Any]] | None = None,
    task_key: str | None = None,
    requested_by: uuid.UUID | None = None,
    requested_slug: str | None = None,
) -> RoomRequest:
    if action not in ACTIONS:
        raise ValueError(f"room request action must be one of {ACTIONS}, got {action!r}")
    if status not in STATUSES:
        raise ValueError(f"room request status must be one of {STATUSES}, got {status!r}")
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            INSERT INTO room_requests (action, status, target_kind, target, display_name, room_id,
                                       candidates, task_key, requested_by, requested_slug)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}
            """,
            (
                action,
                status,
                target_kind,
                target,
                display_name,
                room_id,
                Jsonb(candidates) if candidates is not None else None,
                task_key,
                requested_by,
                requested_slug,
            ),
        )
        return RoomRequest(**await cur.fetchone())


async def get(conn: psycopg.AsyncConnection, request_id: int) -> RoomRequest | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM room_requests WHERE id = %s", (request_id,))
        row = await cur.fetchone()
    return RoomRequest(**row) if row else None


async def count_recent_opens(conn: psycopg.AsyncConnection, seconds: int) -> int:
    """Open and invite rows of any status created in the last `seconds` (the hourly cap)."""
    cur = await conn.execute(
        "SELECT count(*) FROM room_requests WHERE action IN ('open', 'invite')"
        " AND created_at > now() - make_interval(secs => %s)",
        (seconds,),
    )
    return (await cur.fetchone())[0]


async def has_open_accept(conn: psycopg.AsyncConnection, room_id: str) -> bool:
    """Whether the room has an accept row that is not finished yet (confirmed or working)."""
    cur = await conn.execute(
        "SELECT 1 FROM room_requests WHERE action = 'accept' AND room_id = %s"
        " AND status IN ('confirmed', 'working') LIMIT 1",
        (room_id,),
    )
    return await cur.fetchone() is not None


async def attach_decision(
    conn: psycopg.AsyncConnection, request_id: int, task_key: str, decision_id: int
) -> bool:
    """Attach the taskboard decision, once, to a row waiting for confirmation."""
    cur = await conn.execute(
        "UPDATE room_requests SET task_key = %s, decision_id = %s, updated_at = now()"
        " WHERE id = %s AND status = 'pending_confirmation' AND decision_id IS NULL",
        (task_key, decision_id, request_id),
    )
    return cur.rowcount == 1


async def confirm(
    conn: psycopg.AsyncConnection, request_id: int, decision_id: int, approved: bool
) -> bool:
    """pending_confirmation to confirmed (approved) or rejected, only with the attached
    decision id."""
    cur = await conn.execute(
        "UPDATE room_requests SET status = %s, reason = %s, confirmed_at = now(),"
        " updated_at = now()"
        " WHERE id = %s AND status = 'pending_confirmation' AND decision_id = %s",
        (
            "confirmed" if approved else "rejected",
            None if approved else "user_declined",
            request_id,
            decision_id,
        ),
    )
    return cur.rowcount == 1


async def _claim_ids(conn: psycopg.AsyncConnection, status: str) -> list[int]:
    cur = await conn.execute(
        "SELECT id FROM room_requests WHERE status = %s ORDER BY id FOR UPDATE SKIP LOCKED",
        (status,),
    )
    return [row[0] for row in await cur.fetchall()]


async def claim_resolving(conn: psycopg.AsyncConnection) -> list[RoomRequest]:
    """Resolving rows, oldest first, locked for the caller's transaction (SKIP LOCKED); they
    stay `resolving` until `resolved`, `to_candidates` or `fail` moves them."""
    return [await get(conn, request_id) for request_id in await _claim_ids(conn, "resolving")]


async def resolved(
    conn: psycopg.AsyncConnection,
    request_id: int,
    *,
    target_kind: str,
    target: str,
    display_name: str | None,
) -> bool:
    return await _move(
        conn,
        request_id,
        "resolving",
        "pending_confirmation",
        target_kind=target_kind,
        target=target,
        display_name=display_name,
    )


async def to_candidates(
    conn: psycopg.AsyncConnection, request_id: int, candidates: list[dict[str, Any]]
) -> bool:
    return await _move(conn, request_id, "resolving", "candidates", candidates=Jsonb(candidates))


async def fail(conn: psycopg.AsyncConnection, request_id: int, frm: str, reason: str) -> bool:
    return await _move(conn, request_id, frm, "failed", reason=reason)


async def claim_confirmed(conn: psycopg.AsyncConnection) -> list[RoomRequest]:
    """Move every confirmed row to working, oldest first; rows another claimer holds are
    skipped."""
    claimed = []
    for request_id in await _claim_ids(conn, "confirmed"):
        if await _move(conn, request_id, "confirmed", "working"):
            claimed.append(await get(conn, request_id))
    return claimed


async def done(
    conn: psycopg.AsyncConnection, request_id: int, room_id: str | None, reason: str | None = None
) -> bool:
    return await _move(
        conn, request_id, "working", "done", room_id=room_id, reason=reason, done_at=_now()
    )


async def release(conn: psycopg.AsyncConnection, request_id: int) -> bool:
    """working back to confirmed: retried on the next tick."""
    return await _move(conn, request_id, "working", "confirmed")


async def joinable_room_ids(conn: psycopg.AsyncConnection) -> set[str]:
    """Rooms the bot opened itself (open rows in working or done with a room id): their
    invites are joined at once."""
    cur = await conn.execute(
        "SELECT DISTINCT room_id FROM room_requests"
        " WHERE action = 'open' AND status IN ('working', 'done') AND room_id IS NOT NULL"
    )
    return {row[0] for row in await cur.fetchall()}
