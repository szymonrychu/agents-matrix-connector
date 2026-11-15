"""The attachment request queue: an agent asks for the file of a past chat message.

The api inserts `queued` rows (`POST /chat/v1/attachments`); the bot attachment fetcher is the
only one that moves them: `queued` to `fetching` (`claim_next`), then `fetching` to `sent`,
`failed` or `refused` (`finish`), and a restart fails what was `fetching` (`fail_interrupted`).
Every move is a status-guarded update; writers take a connection and never commit, so the
caller owns the transaction.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from agents_common.db.queue import claim, transition
from psycopg.rows import dict_row

INTERRUPTED = "interrupted"
"""The reason of a `fetching` request a restart cut off."""
SETTLED = ("sent", "failed", "refused")
_FINISHED = frozenset(SETTLED)
_COLUMNS = (
    "id, session_id, slug, room_id, inbound_id, status, reason, message_id, created_at,"
    " claimed_at, done_at"
)
_TABLE = "attachment_requests"
_REF = re.compile(r"a([1-9][0-9]*)", re.ASCII)
_BIGINT_MAX = 2**63 - 1


@dataclass(frozen=True)
class AttachmentRequest:
    id: int
    session_id: uuid.UUID
    slug: str | None
    room_id: str
    inbound_id: int
    status: str
    reason: str | None
    message_id: str | None
    created_at: datetime
    claimed_at: datetime | None
    done_at: datetime | None


def parse_request_ref(ref: str) -> int | None:
    """`a<n>` (ASCII digits, no leading zero, n at most 2**63-1) to n; None for anything else."""
    match = _REF.fullmatch(ref)
    if match is None:
        return None
    number = int(match.group(1))
    return number if number <= _BIGINT_MAX else None


async def insert(
    conn: psycopg.AsyncConnection,
    *,
    session_id: uuid.UUID | str,
    slug: str | None,
    room_id: str,
    inbound_id: int,
) -> int | None:
    """Queue a request; None when one for the same message is already queued or fetching."""
    cur = await conn.execute(
        "INSERT INTO attachment_requests (session_id, slug, room_id, inbound_id, status)"
        " VALUES (%s, %s, %s, %s, 'queued')"
        " ON CONFLICT (inbound_id) WHERE status IN ('queued', 'fetching') DO NOTHING"
        " RETURNING id",
        (session_id, slug, room_id, inbound_id),
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def get(conn: psycopg.AsyncConnection, request_id: int) -> AttachmentRequest | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM {_TABLE} WHERE id = %s", (request_id,))
        row = await cur.fetchone()
    return AttachmentRequest(**row) if row else None


async def count_recent(conn: psycopg.AsyncConnection, room_id: str, seconds: int) -> int:
    """Requests of the room created in the last `seconds`, whatever their status."""
    cur = await conn.execute(
        "SELECT count(*) FROM attachment_requests"
        " WHERE room_id = %s AND created_at >= now() - make_interval(secs => %s)",
        (room_id, seconds),
    )
    return (await cur.fetchone())[0]


async def claim_next(conn: psycopg.AsyncConnection, now: datetime) -> AttachmentRequest | None:
    """The oldest queued request, moved to `fetching` with `claimed_at = now`; None when
    nothing is queued or another claimer holds it (SKIP LOCKED)."""
    row = await claim(
        conn,
        "SELECT id FROM attachment_requests WHERE status = 'queued' ORDER BY id"
        " LIMIT 1 FOR UPDATE SKIP LOCKED",
    )
    if row is None:
        return None
    if not await transition(conn, _TABLE, row["id"], "queued", "fetching", claimed_at=now):
        return None
    return await get(conn, row["id"])


async def finish(
    conn: psycopg.AsyncConnection,
    request_id: int,
    status: str,
    reason: str | None,
    message_id: str | None = None,
) -> bool:
    """`fetching` to `sent`, `failed` or `refused`; any other target is a ValueError."""
    if status not in _FINISHED:
        raise ValueError(f"an attachment request ends as sent, failed or refused, got {status!r}")
    return await transition(
        conn,
        _TABLE,
        request_id,
        "fetching",
        status,
        reason=reason,
        message_id=message_id,
        done_at=datetime.now(UTC),
    )


async def fail_interrupted(conn: psycopg.AsyncConnection) -> int:
    """Every `fetching` request to `failed` (a restart cut it off); the count."""
    cur = await conn.execute(
        "UPDATE attachment_requests SET status = 'failed', reason = %s, done_at = now()"
        " WHERE status = 'fetching'",
        (INTERRUPTED,),
    )
    return cur.rowcount


async def prune(conn: psycopg.AsyncConnection, older_than: datetime) -> int:
    """Delete settled requests created before `older_than`."""
    cur = await conn.execute(
        "DELETE FROM attachment_requests WHERE created_at < %s AND status IN ('sent', 'failed',"
        " 'refused')",
        (older_than,),
    )
    return cur.rowcount
