"""The bot's sync loop: discovery and intake from Matrix into the database (design D4-D7).

Each `/sync` response is applied in one transaction together with its `next_batch`, so a
crash before the commit replays the same response; inbound rows are keyed by event id and
the replay inserts nothing twice. Matrix calls (room state, history, joins) happen outside
the transaction so a slow homeserver never holds it open.

A joined room whose timeline arrives `limited` in an incremental sync (more events than the
filter's limit since the last sync, for example after downtime) is backfilled: its history
is paged backwards from `prev_batch` until an event already in `inbound`, the bot's own
join, or `MATRIX_BACKFILL_MAX_EVENTS` events, and the recovered messages go through intake
oldest first, ahead of the timeline, in that sync's transaction.

A room's canonical alias is stored only once the room directory resolves it to that room, so
a room cannot claim another room's alias and match its `alias` rule. A transport error keeps
the stored alias and the check runs again with the room's next state read, on its next sync.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import psycopg
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import discovery, policy
from agents_matrix_connector.clock import utcnow
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

BACKFILL_PAGE = 100
BACKFILL_FILTER = {"types": ["m.room.message", "m.room.member"]}

BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0

Members = dict[str, tuple[str | None, str]]


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


def _is_own_join(event: Mapping, user_id: str) -> bool:
    """`user_id` joining the room, as opposed to changing its profile while joined."""
    if not _is_join_of(event, user_id):
        return False
    unsigned = event.get("unsigned")
    previous = unsigned.get("prev_content") if isinstance(unsigned, dict) else None
    return not isinstance(previous, dict) or previous.get("membership") != "join"


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


def _messages_oldest_first(newest_first: list[dict]) -> list[dict]:
    return [e for e in reversed(newest_first) if e.get("type") == "m.room.message"]


@dataclass(frozen=True)
class _FetchedState:
    """A joined room's full state read outside the transaction, classified, with its alias
    verified; `alias_pending` means the check failed in transit and the stored alias stays."""

    events: list[dict]
    info: discovery.RoomInfo
    alias_pending: bool


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
        self._bridge_server = settings.user_id.split(":", 1)[1]
        self._alias_pending: set[str] = set()
        self._unverified_logged: set[tuple[str, str]] = set()
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
        recovered = await self._backfill(joined) if since is not None else {}
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
                    conn,
                    room_id,
                    room,
                    fetched.get(room_id),
                    recovered.get(room_id, []),
                    rule_list,
                    since is not None,
                    at,
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

    async def _fetch_state(self, joined: Mapping[str, dict]) -> dict[str, _FetchedState]:
        """Full state of each joined room not yet classified, whose room info changed or
        whose alias check is pending."""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT room_id FROM rooms WHERE kind IS NOT NULL AND room_id = ANY(%s)",
                (list(joined),),
            )
            classified = {row[0] for row in await cur.fetchall()}
        fetched: dict[str, _FetchedState] = {}
        for room_id, room in joined.items():
            changed = any(e.get("type") in ROOM_INFO_TYPES for e in _state_events(room))
            if room_id in classified and not changed and room_id not in self._alias_pending:
                continue
            try:
                events = await self._matrix.room_state(room_id)
            except (MatrixAuthError, MatrixUnavailable):
                raise
            except MatrixError as exc:
                log.warning("cannot read the state of room %s, not classified: %s", room_id, exc)
                continue
            info = discovery.classify(events, self._self_ids, self._bridge_bots)
            alias = await self._verified_alias(room_id, info.alias)
            fetched[room_id] = _FetchedState(
                events, replace(info, alias=alias), room_id in self._alias_pending
            )
        return fetched

    async def _verified_alias(self, room_id: str, alias: str | None) -> str | None:
        """`alias` when the room directory resolves it to `room_id`, else None. A transport
        error marks the room's alias check pending (the stored alias stays)."""
        self._alias_pending.discard(room_id)
        if alias is None:
            return None
        try:
            resolved = await self._matrix.resolve_alias(alias)
        except MatrixAuthError:
            raise
        except MatrixUnavailable as exc:
            log.warning(
                "cannot resolve alias %s of room %s, keeping the stored alias: %s",
                alias,
                room_id,
                exc,
            )
            self._alias_pending.add(room_id)
            return None
        except MatrixError:
            resolved = None
        if resolved == room_id:
            return alias
        if (room_id, alias) not in self._unverified_logged:
            self._unverified_logged.add((room_id, alias))
            log.warning("room %s has an unverified canonical alias %s, not stored", room_id, alias)
        return None

    async def _backfill(self, joined: Mapping[str, dict]) -> dict[str, list[dict]]:
        """The missed messages of each joined room whose timeline is limited, oldest first."""
        recovered: dict[str, list[dict]] = {}
        for room_id, room in joined.items():
            block = room.get("timeline")
            if not isinstance(block, dict) or block.get("limited") is not True:
                continue
            prev_batch = block.get("prev_batch")
            if not isinstance(prev_batch, str):
                log.warning(
                    "limited timeline of room %s has no prev_batch, not backfilled", room_id
                )
                continue
            if any(_is_own_join(e, self._settings.user_id) for e in _events(room, "timeline")):
                continue
            try:
                recovered[room_id] = await self._backfill_room(room_id, prev_batch)
            except (MatrixAuthError, MatrixUnavailable):
                raise
            except MatrixError as exc:
                log.warning("cannot read the history of room %s, not backfilled: %s", room_id, exc)
        return recovered

    async def _backfill_room(self, room_id: str, token: str) -> list[dict]:
        cap = self._settings.backfill_max_events
        read: list[dict] = []
        count = 0
        while count < cap:
            page = await self._matrix.room_messages(
                room_id,
                token,
                direction="b",
                limit=min(BACKFILL_PAGE, cap - count),
                filter=BACKFILL_FILTER,
            )
            chunk = page.get("chunk")
            events = [e for e in chunk if isinstance(e, dict)] if isinstance(chunk, list) else []
            count += len(events)
            recorded = await self._recorded([e.get("event_id") for e in events])
            for event in events:
                if event.get("event_id") in recorded or _is_own_join(event, self._settings.user_id):
                    return _messages_oldest_first(read)
                read.append(event)
            end = page.get("end")
            if not events or not isinstance(end, str):
                return _messages_oldest_first(read)
            token = end
        log.warning(
            "backfill of room %s stopped at MATRIX_BACKFILL_MAX_EVENTS after %d events,"
            " older messages are not processed",
            room_id,
            count,
        )
        return _messages_oldest_first(read)

    async def _recorded(self, event_ids: list) -> set[str]:
        ids = [event_id for event_id in event_ids if isinstance(event_id, str)]
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT event_id FROM inbound WHERE event_id = ANY(%s)", (ids,)
            )
            return {row[0] for row in await cur.fetchall()}

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
        return discovery.facebook_id(user_id, self._puppet, self._bridge_server)

    async def _apply_joined(
        self,
        conn: psycopg.AsyncConnection,
        room_id: str,
        room: dict,
        fetched: _FetchedState | None,
        recovered: list[dict],
        rule_list: list[policy.Rule],
        process_messages: bool,
        at: datetime,
    ) -> None:
        cur = await conn.execute(
            "SELECT membership, alias FROM rooms WHERE room_id = %s", (room_id,)
        )
        before = await cur.fetchone()
        newly_joined = before is None or before[0] != "join"
        if fetched is not None:
            info = fetched.info
            if fetched.alias_pending:
                info = replace(info, alias=before[1] if before else None)
            await directory.upsert_room(conn, room_id, membership="join", info=info, at=at)
            members = _memberships(fetched.events)
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
        for event in recovered + timeline:
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
        rule_id: int | None = None
        if isinstance(parsed, discovery.Unsupported):
            status, reason = "ignored", UNSUPPORTED_CONTENT
        else:
            mentioned = discovery.mentions_self(parsed, self._self_ids, room.self_names)
            status, reason, rule_id = await self._decide(
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
            rule_id=rule_id,
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
    ) -> tuple[str, str | None, int | None]:
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
            return "ignored", decision.reason, decision.rule_id
        trigger = discovery.effective_trigger(
            room.kind or "", room.member_count, room.trigger, self._settings.group_trigger
        )
        if trigger == "mention" and not mentioned:
            return "ignored", NOT_MENTIONED, decision.rule_id
        if await inbound.count_new(conn, room_id) >= self._settings.max_queued_per_room:
            return "ignored", QUEUE_FULL, decision.rule_id
        return "new", None, decision.rule_id
