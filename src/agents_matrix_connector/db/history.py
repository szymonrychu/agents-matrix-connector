"""The merged chat history feed of one room (read only, SQL only).

Three sources make the feed, each with its own id letter: `m<n>` inbound rows, `s<n>` agent sends
of kind message and `o<n>` worker replies and notices that belong to no send. One `UNION ALL`
statement reads a page, ordered by `(at, kind, id)`: inbound rows by `COALESCE(origin_ts,
received_at)`, sends and outbound rows by `created_at` (not `sent_at`, so a row keeps its place
while it is delivered). Paging is keyset on that triple. The mxc url is never selected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg

Kind = Literal["m", "s", "o"]
Cursor = tuple[datetime, str, int]
"""A feed position: (at, kind letter, id)."""

_REF = re.compile(r"([mso])([1-9][0-9]*)", re.ASCII)
_BIGINT_MAX = 2**63 - 1

_DENIED = "(COALESCE(i.reason, '') = 'not_allowed' OR COALESCE(i.reason, '') LIKE 'denied:%%')"
"""An inbound row the access policy refused (reason `not_allowed` or `denied: <kind>`)."""

_REPLY_TO = """CASE WHEN {event} IS NULL THEN NULL ELSE COALESCE(
    (SELECT 'm' || ri.id FROM inbound ri WHERE ri.room_id = {room} AND ri.event_id = {event}),
    (SELECT CASE WHEN ro.send_id IS NULL THEN 'o' || ro.id ELSE 's' || ro.send_id END
       FROM outbound ro WHERE ro.room_id = {room} AND ro.event_id = {event}
       ORDER BY ro.id LIMIT 1),
    'unknown') END"""
"""The feed id an event id of the same room resolves to: an inbound row (`m<n>`), a send's
outbound event (`s<n>`), a worker reply or notice (`o<n>`), else `unknown`. `{event}` and `{room}`
are column expressions, never values."""

_M_AT = "COALESCE(i.origin_ts, i.received_at)"
_M_SELECT = f"""
    SELECT 'm' AS kind, i.id, {_M_AT} AS at, i.sender, i.sender_name, i.body, i.status, i.reason,
           {_REPLY_TO.format(event="i.reply_to_event_id", room="i.room_id")} AS reply_to,
           i.edited_at IS NOT NULL AS edited, i.redacted, i.media_kind, i.media_mime,
           i.media_size, i.media_name, 0 AS files
      FROM inbound i WHERE i.room_id = %(room)s"""
_S_SELECT = """
    SELECT 's', s.id, s.created_at, NULL, NULL, s.text, s.status, s.reason, s.reply_to,
           false, false, NULL, NULL, NULL::bigint, NULL,
           (SELECT count(*) FROM send_files f WHERE f.send_id = s.id)::int
      FROM sends s WHERE s.room_id = %(room)s AND s.kind = 'message'"""
_O_SELECT = f"""
    SELECT 'o', o.id, o.created_at, NULL, NULL, o.body, o.status, o.reason,
           {_REPLY_TO.format(event="o.relates_to->'m.in_reply_to'->>'event_id'", room="o.room_id")},
           false, false, NULL, NULL, NULL::bigint, NULL, 0
      FROM outbound o
     WHERE o.room_id = %(room)s AND o.send_id IS NULL AND o.kind IN ('reply', 'notice')"""


@dataclass(frozen=True)
class FeedRow:
    kind: Kind
    id: int
    at: datetime
    sender: str | None
    sender_name: str | None
    body: str | None
    status: str
    reason: str | None
    reply_to: str | None
    """`m<n>`, `s<n>` or `o<n>`; `unknown` when the target is not stored; None without one."""
    edited: bool
    redacted: bool
    media_kind: str | None
    media_mime: str | None
    media_size: int | None
    media_name: str | None
    files: int


def parse_ref(ref: str) -> tuple[Kind, int] | None:
    """The one parser of feed ids: `m<n>`, `s<n>` or `o<n>` (ASCII digits, no leading zero,
    n at most 2**63-1) to its letter and id; None for anything else."""
    match = _REF.fullmatch(ref)
    if match is None:
        return None
    number = int(match.group(2))
    if number > _BIGINT_MAX:
        return None
    letter = match.group(1)
    kind: Kind = "m" if letter == "m" else "s" if letter == "s" else "o"
    return kind, number


_POSITION = {
    "m": f"SELECT {_M_AT} FROM inbound i WHERE i.id = %s AND i.room_id = %s",
    "s": "SELECT created_at FROM sends WHERE id = %s AND room_id = %s AND kind = 'message'",
    "o": "SELECT created_at FROM outbound WHERE id = %s AND room_id = %s AND send_id IS NULL"
    " AND kind IN ('reply', 'notice')",
}


async def position(conn: psycopg.AsyncConnection, room_id: str, ref: str) -> Cursor | None:
    """The feed position of `ref` in the room; None for a malformed id, an unknown id, an id
    of another room and an id that is not in the feed (a reaction send, an outbound row of a
    send or of kind agent or reaction)."""
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    kind, row_id = parsed
    cur = await conn.execute(_POSITION[kind], (row_id, room_id))
    row = await cur.fetchone()
    return None if row is None else (row[0], kind, row_id)


def _after_cursor(at: str, kind: str, id_: str, op: str) -> str:
    return f" AND ({at}, '{kind}'::text, {id_}) {op} (%(c_at)s, %(c_kind)s::text, %(c_id)s)"


async def page(
    conn: psycopg.AsyncConnection,
    room_id: str,
    *,
    before: Cursor | None,
    after: Cursor | None,
    sender_name: str | None,
    sender_digest: str | None,
    limit: int,
) -> tuple[list[FeedRow], bool]:
    """Up to `limit` feed rows in ascending order and whether more exist in the paging
    direction: older than `before`, newer than `after`, else the newest page (more = older).
    A sender filter (`sender_name` case-insensitive, or `sender_digest`, the 12 hex characters
    of the sender's sha256) returns inbound rows only and never denied ones."""
    if before is not None and after is not None:
        raise ValueError("give at most one of before and after")
    params: dict[str, Any] = {"room": room_id, "limit": limit + 1}
    cursor = before or after
    op = "<" if after is None else ">"
    if cursor is not None:
        params.update(c_at=cursor[0], c_kind=cursor[1], c_id=cursor[2])

    m_sql = _M_SELECT
    if cursor is not None:
        m_sql += _after_cursor(_M_AT, "m", "i.id", op)
    filtered = sender_name is not None or sender_digest is not None
    if sender_name is not None:
        m_sql += " AND lower(i.sender_name) = lower(%(name)s)"
        params["name"] = sender_name
    if sender_digest is not None:
        m_sql += " AND left(encode(sha256(convert_to(i.sender, 'UTF8')), 'hex'), 12) = %(digest)s"
        params["digest"] = sender_digest
    if filtered:
        m_sql += f" AND NOT {_DENIED}"
    branches = [m_sql]
    if not filtered:
        s_sql, o_sql = _S_SELECT, _O_SELECT
        if cursor is not None:
            s_sql += _after_cursor("s.created_at", "s", "s.id", op)
            o_sql += _after_cursor("o.created_at", "o", "o.id", op)
        branches += [s_sql, o_sql]

    direction = "ASC" if after is not None else "DESC"
    sql = (
        "SELECT * FROM (" + " UNION ALL ".join(branches) + ") feed"
        f" ORDER BY at {direction}, kind {direction}, id {direction} LIMIT %(limit)s"
    )
    cur = await conn.execute(sql, params)
    rows = [FeedRow(*row) for row in await cur.fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    if after is None:
        rows.reverse()
    return rows, more
