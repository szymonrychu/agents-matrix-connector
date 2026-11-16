"""The connector's record of approval messages and digests it posted.

The bot is the only writer. Writers take a connection and never commit, so the caller owns the
transaction; every state change is a status-guarded update.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import psycopg
from psycopg.types.json import Jsonb

from agents_matrix_connector.approvals import digest_verdict, reaction_verdict, text_verdict
from agents_matrix_connector.discovery import ApprovalEvent


@dataclass(frozen=True)
class Target:
    kind: Literal["approval", "digest"]
    decision_ids: tuple[int, ...]


async def posted_ids(conn: psycopg.AsyncConnection) -> set[int]:
    cur = await conn.execute("SELECT decision_id FROM approval_messages")
    return {row[0] for row in await cur.fetchall()}


async def record_message(
    conn: psycopg.AsyncConnection, decision_id: int, room_id: str, event_id: str
) -> None:
    await conn.execute(
        "INSERT INTO approval_messages (decision_id, room_id, event_id, status)"
        " VALUES (%s, %s, %s, 'posted') ON CONFLICT (decision_id) DO NOTHING",
        (decision_id, room_id, event_id),
    )


async def record_digest(
    conn: psycopg.AsyncConnection, room_id: str, event_id: str, decision_ids: list[int]
) -> int:
    cur = await conn.execute(
        "INSERT INTO approval_digests (room_id, event_id, decision_ids) VALUES (%s, %s, %s)"
        " ON CONFLICT (event_id) DO UPDATE SET event_id = EXCLUDED.event_id RETURNING id",
        (room_id, event_id, decision_ids),
    )
    row = await cur.fetchone()
    assert row is not None
    await conn.execute(
        "UPDATE approval_messages SET digest_id = %s, updated_at = now()"
        " WHERE decision_id = ANY(%s) AND digest_id IS NULL",
        (row[0], decision_ids),
    )
    return row[0]


async def close_missing(conn: psycopg.AsyncConnection, open_ids: set[int]) -> int:
    cur = await conn.execute(
        "UPDATE approval_messages SET status = 'closed', updated_at = now()"
        " WHERE status = 'posted' AND NOT (decision_id = ANY(%s::bigint[]))",
        (sorted(open_ids),),
    )
    return cur.rowcount


async def find_target(conn: psycopg.AsyncConnection, event_id: str) -> Target | None:
    cur = await conn.execute(
        "SELECT decision_id FROM approval_messages WHERE event_id = %s", (event_id,)
    )
    row = await cur.fetchone()
    if row is not None:
        return Target("approval", (row[0],))
    cur = await conn.execute(
        "SELECT decision_ids FROM approval_digests WHERE event_id = %s", (event_id,)
    )
    row = await cur.fetchone()
    if row is not None:
        return Target("digest", tuple(row[0]))
    return None


def _verdict(target: Target, event: ApprovalEvent) -> str | None:
    if target.kind == "digest":
        return digest_verdict(event.text) if event.text is not None else None
    if event.reaction_key is not None:
        return reaction_verdict(event.reaction_key)
    return text_verdict(event.text) if event.text is not None else None


async def record_answer_event(
    conn: psycopg.AsyncConnection, room_id: str, event: ApprovalEvent
) -> bool:
    """Queue the approver's event as a pending answer when it targets an event the connector
    posted and carries an unambiguous verdict; False (not an answer) otherwise."""
    target = await find_target(conn, event.target_event_id)
    if target is None:
        return False
    verdict = _verdict(target, event)
    if verdict is None:
        return False
    await conn.execute(
        "INSERT INTO approval_answers (event_id, room_id, sender, target_event_id, verdict,"
        " status) VALUES (%s, %s, %s, %s, %s, 'pending') ON CONFLICT (event_id) DO NOTHING",
        (event.event_id, room_id, event.sender, event.target_event_id, verdict),
    )
    return True


@dataclass(frozen=True)
class PendingAnswer:
    event_id: str
    room_id: str
    verdict: str
    decision_ids: tuple[int, ...]


async def claim_pending_answers(
    conn: psycopg.AsyncConnection, limit: int = 20
) -> list[PendingAnswer]:
    """Pending answers, oldest first, with the decision ids of their target."""
    cur = await conn.execute(
        "SELECT event_id, room_id, verdict, target_event_id FROM approval_answers"
        " WHERE status = 'pending' ORDER BY created_at, event_id LIMIT %s",
        (limit,),
    )
    out = []
    for event_id, room_id, verdict, target_event_id in await cur.fetchall():
        target = await find_target(conn, target_event_id)
        ids = target.decision_ids if target is not None else ()
        out.append(PendingAnswer(event_id, room_id, verdict, ids))
    return out


async def finish_answer(conn: psycopg.AsyncConnection, event_id: str, results: list[dict]) -> None:
    """Mark the answer done with the taskboard's per-item results, and the approvals it
    answered as answered."""
    await conn.execute(
        "UPDATE approval_answers SET status = 'done', done_at = now(), results = %s"
        " WHERE event_id = %s AND status = 'pending'",
        (Jsonb(results), event_id),
    )
    answered = sorted(
        r["id"] for r in results if r.get("result") == "answered" and isinstance(r.get("id"), int)
    )
    if answered:
        await conn.execute(
            "UPDATE approval_messages SET status = 'answered', updated_at = now()"
            " WHERE status = 'posted' AND decision_id = ANY(%s::bigint[])",
            (answered,),
        )
