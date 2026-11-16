"""Seeders of the three history feed sources (inbound, sends, outbound) for the history tests."""

import json
import uuid
from datetime import datetime

ALICE = "@facebook_111:x"
BOB = "@facebook_222:x"


async def add_m(
    conn,
    room: str,
    at: datetime,
    *,
    sender: str = ALICE,
    name: str | None = "Alice A",
    body: str | None = "hello",
    status: str = "handled",
    reason: str | None = None,
    event_id: str | None = None,
    reply_to: str | None = None,
    media: tuple[str, str, int, str] | None = None,
) -> int:
    """An inbound row at `at` (origin_ts); `media` is (kind, mime, size, name)."""
    kind, mime, size, media_name = media or (None, None, None, None)
    cur = await conn.execute(
        "INSERT INTO inbound (event_id, room_id, sender, sender_name, body, status, reason,"
        " origin_ts, reply_to_event_id, media_kind, media_url, media_mime, media_size,"
        " media_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING id",
        (
            event_id or f"$in-{uuid.uuid4().hex}",
            room,
            sender,
            name,
            body,
            status,
            reason,
            at,
            reply_to,
            kind,
            "mxc://x/secret-media" if kind else None,
            mime,
            size,
            media_name,
        ),
    )
    return (await cur.fetchone())[0]


async def add_s(
    conn,
    room: str,
    at: datetime,
    *,
    kind: str = "message",
    text: str | None = "from agent",
    status: str = "sent",
    reply_to: str | None = None,
    event_id: str | None = None,
) -> int:
    """A send created at `at`; with `event_id`, plus its delivered outbound row (kind agent)."""
    cur = await conn.execute(
        "INSERT INTO sends (session_id, kind, room_id, text, key, reply_to, event_id, status,"
        " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            uuid.uuid4(),
            kind,
            room,
            text if kind == "message" else None,
            "+1" if kind == "reaction" else None,
            reply_to,
            event_id,
            status,
            at,
        ),
    )
    send_id = (await cur.fetchone())[0]
    if event_id is not None:
        await add_o(
            conn,
            room,
            at,
            kind="agent" if kind == "message" else "reaction",
            send_id=send_id,
            event_id=event_id,
        )
    return send_id


async def add_o(
    conn,
    room: str,
    at: datetime,
    *,
    kind: str = "reply",
    body: str = "worker reply",
    status: str = "sent",
    send_id: int | None = None,
    event_id: str | None = None,
    reply_to_event: str | None = None,
) -> int:
    relates = {"m.in_reply_to": {"event_id": reply_to_event}} if reply_to_event else None
    cur = await conn.execute(
        "INSERT INTO outbound (room_id, kind, body, txn_id, event_id, status, send_id,"
        " relates_to, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (
            room,
            kind,
            body,
            f"txn-{uuid.uuid4().hex}",
            event_id,
            status,
            send_id,
            json.dumps(relates) if relates else None,
            at,
        ),
    )
    return (await cur.fetchone())[0]
