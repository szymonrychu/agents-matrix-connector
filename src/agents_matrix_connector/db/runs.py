"""Agent runs per room and the outbound queue (bot worker is the only writer).

Outbound rows are the replies and notices of runs (`run_id`) and the messages and reactions of
agent sends (`send_id`); each carries its Matrix event type and, for a quoted reply or a
reaction, its `m.relates_to` relation.

Run and outbound rows change status only through status-guarded UPDATEs. Writers that take a
connection never commit, so the caller owns the transaction; `PgRunStore` takes the pool and
commits each write, as the agents-common `RunStore` protocol expects.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from agents_common.db.queue import transition
from agents_common.runs import RunId, RunRef, RunStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

_RUN_COLUMNS = (
    "id, room_id, preset_id, kind, slug, session_id, message_id, status, reason,"
    " started_at, finished_at"
)
_OUTBOUND_COLUMNS = (
    "id, room_id, run_id, send_id, kind, event_type, body, relates_to, txn_id, event_id, status,"
    " reason, created_at, sent_at"
)


@dataclass(frozen=True)
class RunRow:
    id: int
    room_id: str
    preset_id: uuid.UUID | None
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
    send_id: int | None
    kind: str
    event_type: str
    body: str
    relates_to: dict[str, Any] | None
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
        async with self._pool.connection() as conn:
            await set_session(conn, int(run_id), session_id)

    async def set_message(self, run_id: RunId, message_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE runs SET message_id = %s WHERE id = %s AND status = 'in-progress'",
                (message_id, int(run_id)),
            )

    async def finish(self, run_id: RunId, status: RunStatus, reason: str | None) -> bool:
        async with self._pool.connection() as conn:
            return await finish(conn, int(run_id), status, reason)


async def create_run(
    conn: psycopg.AsyncConnection, *, room_id: str, preset_id: str | None, kind: str
) -> RunRef:
    """Insert an in-progress run with slug `mx-<id>`. A follow run may have no preset (NULL, and
    `""` in its RunRef, which `deliver` never reads); a first run always has one (CHECK)."""
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
        preset_id=preset_id or "",
        session_id=None,
        message_id=None,
        started_at=started_at,
    )


async def set_session(
    conn: psycopg.AsyncConnection, run_id: int, session_id: uuid.UUID | str
) -> str | None:
    """Store the session of an in-progress run; its slug, or None when it is not in progress."""
    cur = await conn.execute(
        "UPDATE runs SET session_id = %s::uuid WHERE id = %s AND status = 'in-progress'"
        " RETURNING coalesce(slug, 'mx-' || id)",
        (str(session_id), run_id),
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def has_other_run(
    conn: psycopg.AsyncConnection,
    session_id: uuid.UUID | str,
    room_id: str,
    exclude_run_id: int,
) -> bool:
    """Whether the session has a run in the room other than `exclude_run_id`, of any status."""
    cur = await conn.execute(
        "SELECT EXISTS (SELECT 1 FROM runs"
        " WHERE session_id = %s::uuid AND room_id = %s AND id <> %s)",
        (str(session_id), room_id, exclude_run_id),
    )
    return (await cur.fetchone())[0]


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


def _txn_ids(kind: str, run_id: int | None, send_id: int | None, chunk_count: int) -> list[str]:
    """The deterministic transaction ids of a kind: `mxc-` for a run, `mxa-` for a send."""
    if kind in ("reply", "notice"):
        if run_id is None or send_id is not None:
            raise ValueError(f"a {kind} belongs to a run and no send")
    elif kind in ("agent", "reaction"):
        if send_id is None or run_id is not None:
            raise ValueError(f"an {kind} row belongs to a send and no run")
    else:
        raise ValueError(f"outbound kind must be reply, notice, agent or reaction, got {kind!r}")
    if kind in ("notice", "reaction") and chunk_count != 1:
        raise ValueError(f"a {kind} is exactly one chunk")
    if kind == "reply":
        return [f"mxc-{run_id}-{n}" for n in range(1, chunk_count + 1)]
    if kind == "notice":
        return [f"mxc-{run_id}-notice"]
    if kind == "agent":
        return [f"mxa-{send_id}-{n}" for n in range(1, chunk_count + 1)]
    return [f"mxa-{send_id}-r"]


async def add_outbound(
    conn: psycopg.AsyncConnection,
    *,
    room_id: str,
    run_id: int | None = None,
    send_id: int | None = None,
    kind: str,
    chunks: Sequence[str],
    event_type: str = "m.room.message",
    relates_to: dict[str, Any] | None = None,
) -> list[int]:
    """Store the chunks pending, `relates_to` on the first only; re-adding the same chunks
    returns the existing ids. Replies and notices belong to a run, agent messages and reactions
    to a send."""
    txn_ids = _txn_ids(kind, run_id, send_id, len(chunks))
    ids = []
    for n, (txn_id, body) in enumerate(zip(txn_ids, chunks, strict=True)):
        relation = Jsonb(relates_to) if relates_to is not None and n == 0 else None
        await conn.execute(
            """
            INSERT INTO outbound (room_id, run_id, send_id, kind, event_type, body, relates_to,
                                  txn_id, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (txn_id) DO NOTHING
            """,
            (room_id, run_id, send_id, kind, event_type, body, relation, txn_id),
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


async def fail_pending_of_send(conn: psycopg.AsyncConnection, send_id: int, reason: str) -> int:
    """Fail every still-pending row of the send; the number of rows failed."""
    cur = await conn.execute(
        "SELECT id FROM outbound WHERE send_id = %s AND status = 'pending' ORDER BY id",
        (send_id,),
    )
    failed = 0
    for (outbound_id,) in await cur.fetchall():
        failed += await mark_failed(conn, outbound_id, reason)
    return failed


async def count_sent_of_send(conn: psycopg.AsyncConnection, send_id: int) -> int:
    cur = await conn.execute(
        "SELECT count(*) FROM outbound WHERE send_id = %s AND status = 'sent'", (send_id,)
    )
    return (await cur.fetchone())[0]


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
