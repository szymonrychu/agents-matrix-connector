"""The bot's sender step: agent sends from the api's queue to outbound rows (design D7, D8, D9).

Each tick first claims every queued send `queued -> sending` in one transaction. Per send, in
that transaction: (a) the room must pass `agent_room_decision` (else `refused` with the policy
reason); (b) the room must be bound to the send's session, or be unbound, flagged
`agents_may_initiate` and the session bound to no room, in which case the claim binds the room
to the session (else `refused` with `room_bound`, `room_not_authorised` or `session_bound`).
An accepted send gets its outbound rows (`mxa-<send>-<n>` message chunks, the quoted reply
relation on the first only, or one `mxa-<send>-r` reaction), which the tracker sends. A refused
send gets no outbound row and changes no binding.

The tick then settles every sending send whose rows have all ended: `sent` with the first row's
event id when all were sent, else `failed` with the first failed row's reason. A send that ends
`failed` or `refused` with none of its rows sent releases the binding it created
(`send_failed`), in the transaction that ends it. Sends never wait for a room's chat run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import policy
from agents_matrix_connector.api.rooms import agent_room_decision
from agents_matrix_connector.chunks import split
from agents_matrix_connector.clock import utcnow
from agents_matrix_connector.db import bindings, directory, rules, runs, sends
from agents_matrix_connector.settings import BotSettings

log = logging.getLogger(__name__)

SEND_FAILED = "send_failed"
EMPTY_TEXT = "empty_text"
ROOM_NOT_ALLOWED = "room_not_allowed"
ROOM_BOUND = "room_bound"
ROOM_NOT_AUTHORISED = "room_not_authorised"
MESSAGE = "m.room.message"
REACTION = "m.reaction"


@dataclass(frozen=True)
class Plan:
    """The outbound rows of a send: their kind, event type, bodies and first-row relation."""

    kind: str
    event_type: str
    chunks: list[str]
    relates_to: dict[str, Any] | None


def outbound_plan(send: sends.SendRow, chunk_chars: int) -> Plan:
    """A message splits into chunks of `chunk_chars` with a quoted reply relation when it
    replies; a reaction is one `m.annotation` of its key on the referenced event."""
    if send.kind == "reaction":
        assert send.key is not None and send.ref_event_id is not None
        relation = {"rel_type": "m.annotation", "event_id": send.ref_event_id, "key": send.key}
        return Plan("reaction", REACTION, [send.key], relation)
    reply = {"m.in_reply_to": {"event_id": send.ref_event_id}} if send.ref_event_id else None
    return Plan("agent", MESSAGE, split(send.text or "", chunk_chars), reply)


def event_content(event_type: str, body: str, relates_to: dict[str, Any] | None) -> dict:
    """The event content of an outbound row (design D8): an `m.text` message, with its
    relation when it has one and no reply fallback, or a reaction that is only its relation."""
    if event_type == MESSAGE:
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if relates_to is not None:
            content["m.relates_to"] = relates_to
        return content
    if event_type == REACTION:
        if relates_to is None:
            raise ValueError("a reaction needs its m.annotation relation")
        return {"m.relates_to": relates_to}
    raise ValueError(f"outbound event type must be {MESSAGE} or {REACTION}, got {event_type!r}")


class RoomPolicy:
    """D4 (a) for agent sends over one pass: the rules are read once and each room's decision
    once. An unknown room is refused like the api refuses it (`room_not_allowed`)."""

    def __init__(self, pool: AsyncConnectionPool, settings: BotSettings):
        self._pool = pool
        self._settings = settings
        self._rules: list[policy.Rule] | None = None
        self._refusals: dict[str, str | None] = {}

    async def refusal(self, room_id: str) -> str | None:
        """None when agents may post to the room, else the policy reason."""
        if room_id not in self._refusals:
            if self._rules is None:
                self._rules = await rules.list_rules(self._pool)
            room = await directory.get_room(self._pool, room_id)
            if room is None:
                self._refusals[room_id] = ROOM_NOT_ALLOWED
            else:
                decision = agent_room_decision(room, self._rules, self._settings)
                self._refusals[room_id] = (
                    None if decision.allowed else decision.reason or ROOM_NOT_ALLOWED
                )
        return self._refusals[room_id]


async def refuse_sending(conn: psycopg.AsyncConnection, send_id: int, reason: str) -> bool:
    """At send time: fail the send's pending rows, move it `sending -> refused` and, when none
    of its rows was sent, release the binding it created. The caller owns the transaction."""
    await runs.fail_pending_of_send(conn, send_id, reason)
    if not await sends.mark_refused(conn, send_id, reason):
        return False
    if await runs.count_sent_of_send(conn, send_id) == 0:
        await bindings.release_for_send(conn, send_id, SEND_FAILED)
    return True


class Sender:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        settings: BotSettings,
        *,
        now: Callable[[], datetime] = utcnow,
    ):
        self._pool = pool
        self._settings = settings
        self._now = now

    async def tick(self) -> None:
        await self._claim()
        await self._settle()

    async def _claim(self) -> None:
        room_policy = RoomPolicy(self._pool, self._settings)
        async with self._pool.connection() as conn, conn.transaction():
            for send in await sends.claim_queued(conn, self._now()):
                refusal = await self._place(conn, send, room_policy)
                if refusal is not None:
                    await sends.mark_refused(conn, send.id, refusal)
                    log.info("send s%s refused at claim: %s", send.id, refusal)

    async def _place(
        self, conn: psycopg.AsyncConnection, send: sends.SendRow, room_policy: RoomPolicy
    ) -> str | None:
        """Re-check (a) and (b), bind an initiation and store the rows; the refusal or None."""
        refusal = await room_policy.refusal(send.room_id)
        if refusal is not None:
            return refusal
        plan = outbound_plan(send, self._settings.reply_chunk_chars)
        if not plan.chunks:
            await sends.mark_failed(conn, send.id, EMPTY_TEXT)
            log.info("send s%s failed at claim: %s", send.id, EMPTY_TEXT)
            return None
        refusal = await self._authorise(conn, send)
        if refusal is not None:
            return refusal
        await runs.add_outbound(
            conn,
            room_id=send.room_id,
            send_id=send.id,
            kind=plan.kind,
            chunks=plan.chunks,
            event_type=plan.event_type,
            relates_to=plan.relates_to,
        )
        return None

    async def _authorise(self, conn: psycopg.AsyncConnection, send: sends.SendRow) -> str | None:
        """(b), binding an unbound flagged room to a session bound to no room (D9)."""
        binding = await bindings.active_for_room(conn, send.room_id)
        if binding is not None:
            return None if binding.session_id == send.session_id else ROOM_BOUND
        setting = await rules.get_room_settings(conn, send.room_id)
        if setting is None or not setting.agents_may_initiate:
            return ROOM_NOT_AUTHORISED
        outcome = await bindings.bind_for_send(
            conn,
            room_id=send.room_id,
            session_id=send.session_id,
            slug=send.slug,
            send_id=send.id,
        )
        if outcome == "bound":
            log.info("send s%s binds room %s to session %s", send.id, send.room_id, send.session_id)
        return None if outcome in ("bound", "already") else outcome

    async def _settle(self) -> None:
        async with self._pool.connection() as conn, conn.transaction():
            for done in await sends.ended(conn):
                if done.failed == 0 and done.first_event_id is not None:
                    await sends.mark_sent(conn, done.id, done.first_event_id)
                    continue
                reason = done.first_failed_reason or SEND_FAILED
                await sends.mark_failed(conn, done.id, reason)
                if done.sent == 0:
                    await bindings.release_for_send(conn, done.id, SEND_FAILED)
                log.info("send s%s failed: %s", done.id, reason)
