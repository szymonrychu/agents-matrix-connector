"""The bot's sync loop: discovery and intake from Matrix into the database (design D4-D7).

Each `/sync` response is applied in one transaction together with its `next_batch`, so a
crash before the commit replays the same response; inbound rows are keyed by event id and
the replay inserts nothing twice. Matrix calls (room state, joins) happen outside the
transaction so a slow homeserver never holds it open.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import discovery, policy
from agents_matrix_connector.db import directory, inbound, rules
from agents_matrix_connector.matrix import (
    MatrixAuthError,
    MatrixClient,
    MatrixError,
    MatrixNotFound,
    MatrixUnavailable,
)
from agents_matrix_connector.settings import BotSettings

log = logging.getLogger(__name__)

EVENT_TYPES = [
    "m.room.message",
    "m.room.member",
    "m.room.name",
    "m.room.canonical_alias",
    "m.room.encryption",
    "m.bridge",
    "uk.half-shot.bridge",
]
ROOM_INFO_TYPES = frozenset(EVENT_TYPES) - {"m.room.message", "m.room.member"}
TIMELINE_LIMIT = 50
_EXCLUDED = {"not_types": ["*"]}
SYNC_FILTER = {
    "presence": _EXCLUDED,
    "account_data": _EXCLUDED,
    "room": {
        "timeline": {"types": EVENT_TYPES, "limit": TIMELINE_LIMIT},
        "state": {"types": EVENT_TYPES},
        "ephemeral": _EXCLUDED,
        "account_data": _EXCLUDED,
    },
}

UNSUPPORTED_CONTENT = "unsupported_content"
NOT_MENTIONED = "not_mentioned"
QUEUE_FULL = "queue_full"
_BODYLESS = frozenset({policy.ROOM_ENCRYPTED, policy.ROOM_INFRASTRUCTURE, UNSUPPORTED_CONTENT})

BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0

Members = dict[str, tuple[str | None, str]]


def utcnow() -> datetime:
    return datetime.now(UTC)


async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Wait `seconds`, or less when `stop` is set first."""
    with suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


def _stores_body(reason: str | None) -> bool:
    return reason is None or not (reason in _BODYLESS or reason.startswith(policy.denied("")))


def _events(room: Mapping, section: str) -> list[dict]:
    block = room.get(section)
    events = block.get("events") if isinstance(block, dict) else None
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


def _state_events(room: Mapping) -> list[dict]:
    """State from the `state` block and the timeline, in order."""
    timeline = [e for e in _events(room, "timeline") if "state_key" in e]
    return _events(room, "state") + timeline


def _memberships(events: list[dict]) -> Members:
    members: Members = {}
    for event in events:
        user_id = event.get("state_key")
        content = event.get("content")
        if event.get("type") != "m.room.member" or not isinstance(user_id, str):
            continue
        if not isinstance(content, dict) or not isinstance(content.get("membership"), str):
            continue
        display_name = content.get("displayname")
        members[user_id] = (
            display_name if isinstance(display_name, str) and display_name else None,
            content["membership"],
        )
    return members


def _is_join_of(event: Mapping, user_id: str) -> bool:
    content = event.get("content")
    return (
        event.get("type") == "m.room.member"
        and event.get("state_key") == user_id
        and isinstance(content, dict)
        and content.get("membership") == "join"
    )


def _after_join(room: Mapping, timeline: list[dict], user_id: str) -> list[dict]:
    """The timeline events after `user_id`'s join: by timeline order when the join is in the
    timeline, else by origin_server_ts against the join in the state block (design D5, no
    replay of pre-join history)."""
    for index in range(len(timeline) - 1, -1, -1):
        if _is_join_of(timeline[index], user_id):
            return timeline[index + 1 :]
    joins = [_timestamp(e) for e in _events(room, "state") if _is_join_of(e, user_id)]
    joined_at = max((ts for ts in joins if ts is not None), default=None)
    if joined_at is None:
        return timeline
    return [e for e in timeline if (ts := _timestamp(e)) is None or ts >= joined_at]


def _timestamp(event: Mapping) -> datetime | None:
    ms = event.get("origin_server_ts")
    if isinstance(ms, int) and not isinstance(ms, bool) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, UTC)
    return None


@dataclass(frozen=True)
class _RoomContext:
    """What intake needs about a room, read once per room per response."""

    kind: str | None
    alias: str | None
    encrypted: bool
    member_count: int
    trigger: str | None
    self_names: tuple[str, ...]


class SyncLoop:
    def __init__(
        self,
        matrix: MatrixClient,
        pool: AsyncConnectionPool,
        settings: BotSettings,
        *,
        now: Callable[[], datetime] = utcnow,
    ):
        self._matrix = matrix
        self._pool = pool
        self._settings = settings
        self._now = now
        self._self_ids = settings.self_ids()
        self._bridge_bots = tuple(settings.bridge_bots)
        self._puppet = re.compile(settings.puppet_pattern)
        self._filter_id: str | None = None
        self.last_sync_at: datetime | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Sync until `stop` is set; back off on an unavailable homeserver. A MatrixAuthError
        propagates so the process exits (design D5)."""
        delay = BACKOFF_INITIAL_SECONDS
        while not stop.is_set():
            try:
                await self.run_once()
            except MatrixUnavailable as exc:
                log.warning("matrix sync unavailable (%s), retrying in %.0fs", exc, delay)
                await _pause(stop, delay)
                delay = min(delay * 2, BACKOFF_MAX_SECONDS)
                continue
            delay = BACKOFF_INITIAL_SECONDS

    async def run_once(self) -> None:
        async with self._pool.connection() as conn:
            since, stored_filter = await directory.get_sync_state(conn)
        self._filter_id = self._filter_id or stored_filter
        if self._filter_id is None:
            self._filter_id = await self._create_filter()
        try:
            response = await self._sync(since)
        except MatrixNotFound as exc:
            log.warning("sync filter %s is gone (%s), creating a new one", self._filter_id, exc)
            self._filter_id = await self._create_filter()
            response = await self._sync(since)
        rooms = response.get("rooms") or {}
        joined: dict[str, dict] = rooms.get("join") or {}
        fetched = await self._fetch_state(joined)
        at = self._now()
        async with self._pool.connection() as conn, conn.transaction():
            rule_list = await rules.list_rules(conn)
            for room_id, room in (rooms.get("invite") or {}).items():
                await directory.upsert_room(
                    conn,
                    room_id,
                    membership="invite",
                    inviter=self._inviter(room),
                    at=at,
                )
            for room_id, room in joined.items():
                await self._apply_joined(
                    conn, room_id, room, fetched.get(room_id), rule_list, since is not None, at
                )
            for room_id in rooms.get("leave") or {}:
                await directory.set_membership(conn, room_id, "leave", at)
            await directory.set_sync_state(
                conn, next_batch=response["next_batch"], filter_id=self._filter_id, at=at
            )
        self.last_sync_at = at
        await self._join_invites()

    # Matrix calls outside the transaction.

    async def _create_filter(self) -> str:
        return await self._matrix.create_filter(self._settings.user_id, SYNC_FILTER)

    async def _sync(self, since: str | None) -> dict:
        return await self._matrix.sync(
            since=since,
            timeout_ms=self._settings.sync_timeout_seconds * 1000,
            filter_id=self._filter_id,
        )

    async def _fetch_state(self, joined: Mapping[str, dict]) -> dict[str, list[dict]]:
        """Full state of each joined room not yet classified or whose room info changed."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT room_id FROM rooms WHERE kind IS NOT NULL AND room_id = ANY(%s)",
                (list(joined),),
            )
            classified = {row[0] for row in await cur.fetchall()}
        fetched: dict[str, list[dict]] = {}
        for room_id, room in joined.items():
            changed = any(e.get("type") in ROOM_INFO_TYPES for e in _state_events(room))
            if room_id in classified and not changed:
                continue
            try:
                fetched[room_id] = await self._matrix.room_state(room_id)
            except (MatrixAuthError, MatrixUnavailable):
                raise
            except MatrixError as exc:
                log.warning("cannot read the state of room %s, not classified: %s", room_id, exc)
        return fetched

    async def _join_invites(self) -> None:
        """Join every pending invite; a failed join stays pending and is retried next sync."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT room_id FROM rooms WHERE membership = 'invite'"
                " ORDER BY discovered_at, room_id"
            )
            invited = [row[0] for row in await cur.fetchall()]
        for room_id in invited:
            try:
                await self._matrix.join(room_id)
            except MatrixAuthError:
                raise
            except MatrixError as exc:
                log.warning("join of room %s failed, retrying on the next sync: %s", room_id, exc)

    # Applying one response, inside its transaction.

    def _inviter(self, room: Mapping) -> str | None:
        for event in _events(room, "invite_state"):
            if (
                event.get("type") == "m.room.member"
                and event.get("state_key") == self._settings.user_id
                and isinstance(event.get("sender"), str)
            ):
                return event["sender"]
        return None

    def _facebook_id(self, user_id: str) -> str | None:
        return discovery.facebook_id(user_id, self._puppet)

    async def _apply_joined(
        self,
        conn: psycopg.AsyncConnection,
        room_id: str,
        room: dict,
        state: list[dict] | None,
        rule_list: list[policy.Rule],
        process_messages: bool,
        at: datetime,
    ) -> None:
        cur = await conn.execute("SELECT membership FROM rooms WHERE room_id = %s", (room_id,))
        before = await cur.fetchone()
        newly_joined = before is None or before[0] != "join"
        if state is not None:
            info = discovery.classify(state, self._self_ids, self._bridge_bots)
            await directory.upsert_room(conn, room_id, membership="join", info=info, at=at)
            members = _memberships(state)
        else:
            await directory.upsert_room(conn, room_id, membership="join", at=at)
            members = _memberships(_state_events(room))
        if members:
            await directory.upsert_members(conn, room_id, members, self._facebook_id, at)
        timeline = _events(room, "timeline")
        if not timeline:
            return
        stamps = [ts for ts in map(_timestamp, timeline) if ts is not None]
        await directory.touch_room(conn, room_id, max(stamps, default=at))
        if not process_messages:
            return
        if newly_joined:
            timeline = _after_join(room, timeline, self._settings.user_id)
        context: _RoomContext | None = None
        for event in timeline:
            parsed = discovery.parse_message(room_id, event, self._self_ids, self._bridge_bots)
            if isinstance(parsed, discovery.Skip):
                continue
            context = context or await self._room_context(conn, room_id)
            await self._intake(conn, room_id, event, parsed, context, rule_list)

    async def _room_context(self, conn: psycopg.AsyncConnection, room_id: str) -> _RoomContext:
        cur = await conn.execute(
            "SELECT kind, alias, encrypted, member_count FROM rooms WHERE room_id = %s",
            (room_id,),
        )
        kind, alias, encrypted, member_count = await cur.fetchone()
        cur = await conn.execute(
            "SELECT display_name FROM room_members WHERE room_id = %s AND user_id = ANY(%s)"
            " AND display_name IS NOT NULL ORDER BY user_id",
            (room_id, sorted(self._self_ids)),
        )
        self_names = tuple(row[0] for row in await cur.fetchall())
        room_settings = await rules.get_room_settings(conn, room_id)
        return _RoomContext(
            kind=kind,
            alias=alias,
            encrypted=encrypted,
            member_count=member_count,
            trigger=room_settings.trigger if room_settings else None,
            self_names=self_names,
        )

    async def _intake(
        self,
        conn: psycopg.AsyncConnection,
        room_id: str,
        event: dict,
        parsed: discovery.Candidate | discovery.Unsupported,
        room: _RoomContext,
        rule_list: list[policy.Rule],
    ) -> None:
        cur = await conn.execute(
            "SELECT display_name, facebook_id FROM users WHERE user_id = %s", (parsed.sender,)
        )
        row = await cur.fetchone()
        sender_name, sender_facebook_id = row if row else (None, self._facebook_id(parsed.sender))
        body: str | None = None
        mentioned = False
        if isinstance(parsed, discovery.Unsupported):
            status, reason = "ignored", UNSUPPORTED_CONTENT
        else:
            mentioned = discovery.mentions_self(parsed, self._self_ids, room.self_names)
            status, reason = await self._decide(
                conn, room_id, parsed, room, rule_list, sender_name, sender_facebook_id, mentioned
            )
            if _stores_body(reason):
                body = discovery.truncate(parsed.body, self._settings.max_inbound_chars)
        await inbound.insert(
            conn,
            event_id=parsed.event_id,
            room_id=room_id,
            sender=parsed.sender,
            sender_name=sender_name,
            body=body,
            mentioned=mentioned,
            origin_ts=_timestamp(event),
            status=status,
            reason=reason,
        )

    async def _decide(
        self,
        conn: psycopg.AsyncConnection,
        room_id: str,
        candidate: discovery.Candidate,
        room: _RoomContext,
        rule_list: list[policy.Rule],
        sender_name: str | None,
        sender_facebook_id: str | None,
        mentioned: bool,
    ) -> tuple[str, str | None]:
        subject = policy.Subject(
            room_id=room_id,
            room_alias=room.alias,
            room_kind=room.kind,
            encrypted=room.encrypted,
            sender_id=candidate.sender,
            sender_facebook_id=sender_facebook_id,
            sender_name=sender_name,
        )
        decision = policy.evaluate(subject, rule_list, self._settings.default_policy)
        if not decision.allowed:
            return "ignored", decision.reason
        trigger = discovery.effective_trigger(
            room.kind or "", room.member_count, room.trigger, self._settings.group_trigger
        )
        if trigger == "mention" and not mentioned:
            return "ignored", NOT_MENTIONED
        if await inbound.count_new(conn, room_id) >= self._settings.max_queued_per_room:
            return "ignored", QUEUE_FULL
        return "new", None
