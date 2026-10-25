"""Agent runs per room and the outbound reply queue (bot worker is the only writer).

Run and outbound rows change status only through status-guarded UPDATEs. Writers that take a
connection never commit, so the caller owns the transaction; `PgRunStore` takes the pool and
commits each write, as the agents-common `RunStore` protocol expects.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from agents_common.db.queue import transition
from agents_common.runs import RunId, RunRef, RunStatus
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_RUN_COLUMNS = (
    "id, room_id, preset_id, kind, slug, session_id, message_id, status, reason,"
    " started_at, finished_at"
)
_OUTBOUND_COLUMNS = (
    "id, room_id, run_id, kind, body, txn_id, event_id, status, reason, created_at, sent_at"
)


@dataclass(frozen=True)
class RunRow:
    id: int
    room_id: str
    preset_id: uuid.UUID
    kind: str
    slug: str | None
    session_id: uuid.UUID | None
    message_id: str | None
    status: str
    reason: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class OutboundRow:
    id: int
    room_id: str
    run_id: int | None
    kind: str
    body: str
    txn_id: str
    event_id: str | None
    status: str
    reason: str | None
    created_at: datetime
    sent_at: datetime | None


class PgRunStore:
    """The agents-common `RunStore`: guarded writes on an in-progress run."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def set_session(self, run_id: RunId, session_id: str) -> None:
        await self._update("session_id = %s::uuid", session_id, run_id)

    async def set_message(self, run_id: RunId, message_id: str) -> None:
        await self._update("message_id = %s", message_id, run_id)

    async def finish(self, run_id: RunId, status: RunStatus, reason: str | None) -> bool:
        async with self._pool.connection() as conn:
            return await finish(conn, int(run_id), status, reason)

    async def _update(self, assignment: str, value: str, run_id: RunId) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE runs SET {assignment} WHERE id = %s AND status = 'in-progress'",
                (value, int(run_id)),
            )


async def create_run(
    conn: psycopg.AsyncConnection, *, room_id: str, preset_id: str, kind: str
) -> RunRef:
    if kind not in ("first", "follow"):
        raise ValueError(f"run kind must be first or follow, got {kind!r}")
    cur = await conn.execute(
        """
        INSERT INTO runs (room_id, preset_id, kind, status)
        VALUES (%s, %s::uuid, %s, 'in-progress') RETURNING id, started_at
        """,
        (room_id, preset_id, kind),
    )
    run_id, started_at = await cur.fetchone()
    slug = f"mx-{run_id}"
    await conn.execute("UPDATE runs SET slug = %s WHERE id = %s", (slug, run_id))
    return RunRef(
        id=run_id,
        slug=slug,
        preset_id=preset_id,
        session_id=None,
        message_id=None,
        started_at=started_at,
    )


async def latest_session(
    conn: psycopg.AsyncConnection, room_id: str
) -> tuple[int, uuid.UUID] | None:
    cur = await conn.execute(
        "SELECT id, session_id FROM runs WHERE room_id = %s AND session_id IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (room_id,),
    )
    row = await cur.fetchone()
    return (row[0], row[1]) if row else None


async def in_progress(conn: psycopg.AsyncConnection) -> list[RunRow]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs WHERE status = 'in-progress' ORDER BY id"
        )
        return [RunRow(**row) for row in await cur.fetchall()]


async def count_first_in_progress(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM runs WHERE status = 'in-progress' AND kind = 'first'"
    )
    return (await cur.fetchone())[0]


async def finish(
    conn: psycopg.AsyncConnection, run_id: int, status: str, reason: str | None = None
) -> bool:
    if status not in ("handled", "failed"):
        raise ValueError(f"finish status must be handled or failed, got {status!r}")
    return await transition(
        conn,
        "runs",
        run_id,
        "in-progress",
        status,
        reason=reason,
        finished_at=datetime.now(UTC),
    )


async def add_outbound(
    conn: psycopg.AsyncConnection,
    *,
    room_id: str,
    run_id: int,
    kind: str,
    chunks: Sequence[str],
) -> list[int]:
    """Store the chunks pending; re-adding the same run's chunks returns the existing ids."""
    if kind == "reply":
        txn_ids = [f"mxc-{run_id}-{n}" for n in range(1, len(chunks) + 1)]
    elif kind == "notice":
        if len(chunks) != 1:
            raise ValueError("a notice is exactly one chunk")
        txn_ids = [f"mxc-{run_id}-notice"]
    else:
        raise ValueError(f"outbound kind must be reply or notice, got {kind!r}")
    ids = []
    for txn_id, body in zip(txn_ids, chunks, strict=True):
        await conn.execute(
            """
            INSERT INTO outbound (room_id, run_id, kind, body, txn_id, status)
            VALUES (%s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (txn_id) DO NOTHING
            """,
            (room_id, run_id, kind, body, txn_id),
        )
        cur = await conn.execute("SELECT id FROM outbound WHERE txn_id = %s", (txn_id,))
        ids.append((await cur.fetchone())[0])
    return ids


async def pending(conn: psycopg.AsyncConnection) -> list[OutboundRow]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_OUTBOUND_COLUMNS} FROM outbound WHERE status = 'pending' ORDER BY id"
        )
        return [OutboundRow(**row) for row in await cur.fetchall()]


async def mark_sent(conn: psycopg.AsyncConnection, outbound_id: int, event_id: str) -> bool:
    return await transition(
        conn,
        "outbound",
        outbound_id,
        "pending",
        "sent",
        event_id=event_id,
        sent_at=datetime.now(UTC),
    )


async def mark_failed(conn: psycopg.AsyncConnection, outbound_id: int, reason: str) -> bool:
    return await transition(conn, "outbound", outbound_id, "pending", "failed", reason=reason)


async def list_outbound(
    pool: AsyncConnectionPool,
    room_id: str,
    *,
    limit: int,
    before_id: int | None = None,
) -> list[OutboundRow]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT {_OUTBOUND_COLUMNS} FROM outbound
            WHERE room_id = %(room)s AND (%(before)s::bigint IS NULL OR id < %(before)s)
            ORDER BY id DESC
            LIMIT %(limit)s
            """,
            {"room": room_id, "before": before_id, "limit": limit},
        )
        return [OutboundRow(**row) for row in await cur.fetchall()]


async def prune(conn: psycopg.AsyncConnection, older_than: datetime) -> int:
    cur = await conn.execute(
        "DELETE FROM runs WHERE finished_at < %s AND status IN ('handled', 'failed')",
        (older_than,),
    )
    runs_deleted = cur.rowcount
    cur = await conn.execute(
        "DELETE FROM outbound WHERE created_at < %s AND status IN ('sent', 'failed')",
        (older_than,),
    )
    return runs_deleted + cur.rowcount
