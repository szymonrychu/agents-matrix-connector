"""The agent send queue.

The api inserts `queued` rows; the bot worker is the only one that moves them, and every move is
a status-guarded update through `agents_common.db.queue.transition`. Writers take a connection
and never commit, so the caller owns the transaction. The room-session binding lives in
`db/bindings.py`, not here.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import psycopg
from agents_common.db.queue import transition
from psycopg.rows import dict_row

_COLUMNS = (
    "id, session_id, slug, kind, room_id, text, key, reply_to, ref_event_id, event_id, status,"
    " reason, created_at, claimed_at, sent_at"
)
_REF = re.compile(r"([ms])([1-9][0-9]*)", re.ASCII)
_BIGINT_MAX = 2**63 - 1


@dataclass(frozen=True)
class SendRow:
    id: int
    session_id: uuid.UUID
    slug: str | None
    kind: str
    room_id: str
    text: str | None
    key: str | None
    reply_to: str | None
    ref_event_id: str | None
    event_id: str | None
    status: str
    reason: str | None
    created_at: datetime
    claimed_at: datetime | None
    sent_at: datetime | None


@dataclass(frozen=True)
class Ended:
    """A sending send whose outbound rows have all ended (none is pending)."""

    id: int
    session_id: uuid.UUID
    sent: int
    first_event_id: str | None
    first_failed_reason: str | None
    failed: int


@dataclass(frozen=True)
class Ref:
    """A resolved `m<n>` (an inbound row) or `s<n>` (a send); the send fields are None for m."""

    source: Literal["inbound", "send"]
    id: int
    room_id: str
    event_id: str | None
    session_id: uuid.UUID | None = None
    kind: str | None = None
    status: str | None = None


def parse_ref(ref: str) -> tuple[Literal["m", "s"], int] | None:
    """`m<n>` or `s<n>` to its letter and id; None for anything else."""
    match = _REF.fullmatch(ref)
    if match is None:
        return None
    number = int(match.group(2))
    if number > _BIGINT_MAX:
        return None
    return ("m" if match.group(1) == "m" else "s"), number


async def insert(
    conn: psycopg.AsyncConnection,
    *,
    session_id: uuid.UUID | str,
    slug: str | None,
    kind: str,
    room_id: str,
    text: str | None,
    key: str | None,
    reply_to: str | None,
    ref_event_id: str | None,
) -> int:
    if kind not in ("message", "reaction"):
        raise ValueError(f"send kind must be message or reaction, got {kind!r}")
    cur = await conn.execute(
        """
        INSERT INTO sends (session_id, slug, kind, room_id, text, key, reply_to, ref_event_id,
                           status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued') RETURNING id
        """,
        (session_id, slug, kind, room_id, text, key, reply_to, ref_event_id),
    )
    return (await cur.fetchone())[0]


async def get(conn: psycopg.AsyncConnection, send_id: int) -> SendRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(f"SELECT {_COLUMNS} FROM sends WHERE id = %s", (send_id,))
        row = await cur.fetchone()
    return SendRow(**row) if row else None


async def claim_queued(conn: psycopg.AsyncConnection, now: datetime | None = None) -> list[SendRow]:
    """Move every queued send to sending, oldest first; a send another claimer holds is skipped.
    `claimed_at` is `now`, else the current time."""
    cur = await conn.execute(
        "SELECT id FROM sends WHERE status = 'queued' ORDER BY id FOR UPDATE SKIP LOCKED"
    )
    claimed = []
    now = now or datetime.now(UTC)
    for (send_id,) in await cur.fetchall():
        if await transition(conn, "sends", send_id, "queued", "sending", claimed_at=now):
            claimed.append(await get(conn, send_id))
    return claimed


async def mark_sent(conn: psycopg.AsyncConnection, send_id: int, event_id: str) -> bool:
    return await transition(
        conn, "sends", send_id, "sending", "sent", event_id=event_id, sent_at=datetime.now(UTC)
    )


async def mark_failed(conn: psycopg.AsyncConnection, send_id: int, reason: str) -> bool:
    return await transition(conn, "sends", send_id, "sending", "failed", reason=reason)


async def mark_refused(conn: psycopg.AsyncConnection, send_id: int, reason: str) -> bool:
    return await transition(conn, "sends", send_id, "sending", "refused", reason=reason)


async def ended(conn: psycopg.AsyncConnection) -> list[Ended]:
    """Every sending send that has outbound rows and none of them pending, oldest first: how
    many were sent and failed, the first row's event id and the first failed row's reason."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT s.id, s.session_id,
                   count(*) FILTER (WHERE o.status = 'sent') AS sent,
                   count(*) FILTER (WHERE o.status = 'failed') AS failed,
                   (array_agg(o.event_id ORDER BY o.id))[1] AS first_event_id,
                   (array_agg(o.reason ORDER BY o.id)
                       FILTER (WHERE o.status = 'failed'))[1] AS first_failed_reason
            FROM sends s JOIN outbound o ON o.send_id = s.id
            WHERE s.status = 'sending'
            GROUP BY s.id, s.session_id
            HAVING count(*) FILTER (WHERE o.status = 'pending') = 0
            ORDER BY s.id
            """
        )
        return [Ended(**row) for row in await cur.fetchall()]


async def count_recent(conn: psycopg.AsyncConnection, session_id: uuid.UUID, seconds: int) -> int:
    """The session's sends of any status and kind created in the last `seconds`."""
    cur = await conn.execute(
        "SELECT count(*) FROM sends"
        " WHERE session_id = %s AND created_at > now() - make_interval(secs => %s)",
        (session_id, seconds),
    )
    return (await cur.fetchone())[0]


async def find_duplicate(
    conn: psycopg.AsyncConnection,
    *,
    session_id: uuid.UUID,
    room_id: str,
    kind: str,
    text: str | None,
    key: str | None,
    reply_to: str | None,
    seconds: int,
) -> SendRow | None:
    """The newest queued, sending or sent send of the session created in the last `seconds`
    whose room, kind, text, key and reply_to equal the given ones exactly (design D5)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM sends"
            " WHERE session_id = %(session)s AND room_id = %(room)s AND kind = %(kind)s"
            " AND text IS NOT DISTINCT FROM %(text)s::text"
            " AND key IS NOT DISTINCT FROM %(key)s::text"
            " AND reply_to IS NOT DISTINCT FROM %(reply_to)s::text"
            " AND status IN ('queued', 'sending', 'sent')"
            " AND created_at > now() - make_interval(secs => %(seconds)s)"
            " ORDER BY id DESC LIMIT 1",
            {
                "session": session_id,
                "room": room_id,
                "kind": kind,
                "text": text,
                "key": key,
                "reply_to": reply_to,
                "seconds": seconds,
            },
        )
        row = await cur.fetchone()
    return SendRow(**row) if row else None


async def resolve(conn: psycopg.AsyncConnection, ref: str) -> Ref | None:
    """`m<n>` to the inbound row's room and event id; `s<n>` to the send's room, session, kind,
    status and delivered event id. None for an unknown or malformed reference."""
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    letter, row_id = parsed
    if letter == "m":
        cur = await conn.execute("SELECT room_id, event_id FROM inbound WHERE id = %s", (row_id,))
        row = await cur.fetchone()
        return None if row is None else Ref("inbound", row_id, row[0], row[1])
    send = await get(conn, row_id)
    if send is None:
        return None
    return Ref(
        "send",
        row_id,
        send.room_id,
        send.event_id,
        session_id=send.session_id,
        kind=send.kind,
        status=send.status,
    )


async def prune(conn: psycopg.AsyncConnection, older_than: datetime) -> int:
    cur = await conn.execute(
        "DELETE FROM sends WHERE created_at < %s AND status IN ('sent', 'failed', 'refused')",
        (older_than,),
    )
    return cur.rowcount
