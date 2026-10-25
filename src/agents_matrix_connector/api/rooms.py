"""Rooms, users, messages and meta routes (design D11)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from agents_common.orchestrator import NotFound, OrchestratorError, OrchestratorUnavailable
from fastapi import APIRouter, Query
from psycopg_pool import AsyncConnectionPool

from agents_matrix_connector import policy
from agents_matrix_connector.api.schemas import (
    AccessView,
    ApiError,
    MemberView,
    MessageView,
    MetaView,
    RoomDetail,
    RoomSettingsIn,
    RoomView,
    SessionView,
    SyncView,
    UserView,
)
from agents_matrix_connector.db import directory, inbound, rules, runs
from agents_matrix_connector.settings import ApiSettings

RoomKind = Literal["dm", "group", "matrix", "infrastructure"]
AccessState = Literal["allowed", "denied", "default"]

_NEUTRAL_ROOM = policy.Subject("", None, None, False, None, None, None)
"""A user outside any room: only the sender rules and the default policy apply."""


def access_view(decision: policy.Decision) -> AccessView:
    if decision.source == "default":
        state = "default"
    else:
        state = "allowed" if decision.allowed else "denied"
    return AccessView(state=state, reason=decision.reason, rule_id=decision.rule_id)


def _room_subject(room: directory.RoomRow) -> policy.Subject:
    return policy.Subject(room.room_id, room.alias, room.kind, room.encrypted, None, None, None)


def _member_subject(room: directory.RoomRow, member: directory.MemberRow) -> policy.Subject:
    return policy.Subject(
        room.room_id,
        room.alias,
        room.kind,
        room.encrypted,
        member.user_id,
        member.facebook_id,
        member.display_name,
    )


def _effective_trigger(
    room: directory.RoomRow, setting: rules.RoomSettings | None, group_trigger: str
) -> str:
    if setting is not None and setting.trigger is not None:
        return setting.trigger
    if room.kind == "dm" or (room.kind == "matrix" and room.member_count <= 2):
        return "all"
    return group_trigger


def _stamp(at: datetime, direction: str, row_id: int) -> tuple[datetime, int, int]:
    """Total order of the merged feed: time, then outbound after inbound, then id."""
    return at, 1 if direction == "out" else 0, row_id


def _parse_cursor(raw: str) -> tuple[str, int]:
    direction, _, number = raw.partition("-")
    if direction not in ("in", "out") or not number.isdigit():
        raise ApiError(422, "invalid_request", "before_id: not a message id")
    return direction, int(number)


def build_rooms_router(pool: AsyncConnectionPool, orchestrator, settings: ApiSettings) -> APIRouter:
    router = APIRouter()

    def room_view(
        room: directory.RoomRow,
        setting: rules.RoomSettings | None,
        rule_list: list[policy.Rule],
    ) -> dict:
        decision = policy.evaluate(_room_subject(room), rule_list, settings.default_policy)
        return {
            "room_id": room.room_id,
            "name": room.name,
            "alias": room.alias,
            "kind": room.kind,
            "encrypted": room.encrypted,
            "message_request": room.message_request,
            "membership": room.membership,
            "member_count": room.member_count,
            "access": access_view(decision),
            "preset_id": setting.preset_id if setting else None,
            "trigger": setting.trigger if setting else None,
            "effective_trigger": _effective_trigger(room, setting, settings.group_trigger),
            "session": (
                SessionView(
                    id=room.session.session_id, slug=room.session.slug, status=room.session.status
                )
                if room.session
                else None
            ),
            "last_event_at": room.last_event_at,
            "queued": room.queued,
        }

    async def detail(room: directory.RoomRow, rule_list: list[policy.Rule]) -> RoomDetail:
        setting = await rules.get_room_settings(pool, room.room_id)
        members = [
            MemberView(
                user_id=m.user_id,
                display_name=m.display_name,
                facebook_id=m.facebook_id,
                membership=m.membership,
                access=access_view(
                    policy.evaluate(_member_subject(room, m), rule_list, settings.default_policy)
                ),
            )
            for m in room.members
        ]
        return RoomDetail(**room_view(room, setting, rule_list), members=members)

    async def require_room(room_id: str) -> directory.RoomRow:
        room = await directory.get_room(pool, room_id)
        if room is None:
            raise ApiError(404, "not_found", f"room {room_id!r} not found")
        return room

    @router.get("/meta", response_model=MetaView)
    async def meta() -> MetaView:
        last = await directory.last_sync_at(pool)
        window = timedelta(seconds=3 * settings.sync_timeout_seconds)
        healthy = last is not None and datetime.now(UTC) - last <= window
        return MetaView(
            user_id=settings.user_id,
            homeserver_url=settings.homeserver_url,
            default_policy=settings.default_policy,
            group_trigger=settings.group_trigger,
            rule_kinds={name: list(kinds) for name, kinds in policy.KINDS.items()},
            sync=SyncView(last_sync_at=last, healthy=healthy),
        )

    @router.get("/rooms", response_model=list[RoomView])
    async def list_rooms(
        kind: RoomKind | None = None, access: AccessState | None = None
    ) -> list[RoomView]:
        rule_list = await rules.list_rules(pool)
        views = []
        for room in await directory.list_rooms(pool, kind):
            view = RoomView(
                **room_view(room, await rules.get_room_settings(pool, room.room_id), rule_list)
            )
            if access is None or view.access.state == access:
                views.append(view)
        return views

    @router.get("/rooms/{room_id}", response_model=RoomDetail)
    async def get_room(room_id: str) -> RoomDetail:
        room = await require_room(room_id)
        return await detail(room, await rules.list_rules(pool))

    @router.put("/rooms/{room_id}/settings", response_model=RoomDetail)
    async def put_settings(room_id: str, body: RoomSettingsIn) -> RoomDetail:
        await require_room(room_id)
        if body.preset_id is not None:
            try:
                await orchestrator.get_preset(body.preset_id)
            except NotFound:
                raise ApiError(
                    422, "unknown_preset", f"preset_id: unknown preset {body.preset_id}"
                ) from None
            except OrchestratorUnavailable:
                raise ApiError(503, "unavailable", "orchestrator unavailable") from None
            except OrchestratorError as exc:
                raise ApiError(502, "orchestrator_error", str(exc)) from None
        await rules.set_room_settings(pool, room_id, preset_id=body.preset_id, trigger=body.trigger)
        return await detail(await require_room(room_id), await rules.list_rules(pool))

    @router.get("/rooms/{room_id}/messages", response_model=list[MessageView])
    async def room_messages(
        room_id: str,
        limit: int = Query(50, ge=1, le=200),
        before_id: str | None = None,
    ) -> list[MessageView]:
        await require_room(room_id)
        cursor = None
        if before_id is not None:
            cursor = await _cursor_stamp(room_id, *_parse_cursor(before_id))

        async def fetch_in(before: int | None, n: int) -> list[inbound.InboundRow]:
            return await inbound.list_messages(pool, room_id=room_id, limit=n, before_id=before)

        async def fetch_out(before: int | None, n: int) -> list[runs.OutboundRow]:
            return await runs.list_outbound(pool, room_id, limit=n, before_id=before)

        ins = await _older(fetch_in, lambda r: _stamp(r.received_at, "in", r.id), cursor, limit)
        outs = await _older(fetch_out, lambda r: _stamp(r.created_at, "out", r.id), cursor, limit)
        feed = [_in_view(r) for r in ins] + [_out_view(r, settings.user_id) for r in outs]
        feed.sort(key=lambda m: _stamp(m.at, m.direction, int(m.id.split("-")[1])), reverse=True)
        return feed[:limit]

    async def _cursor_stamp(room_id: str, direction: str, number: int):
        if direction == "in":
            rows = await inbound.list_messages(pool, room_id=room_id, limit=1, before_id=number + 1)
            if rows and rows[0].id == number:
                return _stamp(rows[0].received_at, "in", number)
        else:
            out_rows = await runs.list_outbound(pool, room_id, limit=1, before_id=number + 1)
            if out_rows and out_rows[0].id == number:
                return _stamp(out_rows[0].created_at, "out", number)
        raise ApiError(422, "invalid_request", "before_id: unknown message")

    @router.get("/users", response_model=list[UserView])
    async def list_users(
        q: str | None = None, limit: int = Query(100, ge=1, le=500)
    ) -> list[UserView]:
        rule_list = await rules.list_rules(pool)
        return [
            UserView(
                user_id=u.user_id,
                display_name=u.display_name,
                facebook_id=u.facebook_id,
                rooms=u.room_ids,
                access=access_view(
                    policy.evaluate(
                        policy.Subject(
                            "",
                            None,
                            None,
                            False,
                            u.user_id,
                            u.facebook_id,
                            u.display_name,
                        ),
                        rule_list,
                        settings.default_policy,
                    )
                ),
            )
            for u in await directory.list_users(pool, q, limit)
        ]

    return router


async def _older(
    fetch: Callable[[int | None, int], Awaitable[list]],
    stamp_of: Callable,
    cursor: tuple | None,
    limit: int,
) -> list:
    """Up to `limit` newest rows strictly older than `cursor`, paging down by id."""
    found: list = []
    before: int | None = None
    while len(found) < limit:
        rows = await fetch(before, limit)
        for row in rows:
            if cursor is None or stamp_of(row) < cursor:
                found.append(row)
                if len(found) == limit:
                    break
        if len(rows) < limit:
            break
        before = rows[-1].id
    return found


def _in_view(row: inbound.InboundRow) -> MessageView:
    return MessageView(
        direction="in",
        id=f"in-{row.id}",
        at=row.received_at,
        sender=row.sender,
        sender_name=row.sender_name,
        body=row.body,
        status=row.status,
        reason=row.reason,
        run_id=row.run_id,
    )


def _out_view(row: runs.OutboundRow, bot_user_id: str) -> MessageView:
    return MessageView(
        direction="out",
        id=f"out-{row.id}",
        at=row.created_at,
        sender=bot_user_id,
        sender_name=None,
        body=row.body,
        status=row.status,
        reason=row.reason,
        run_id=row.run_id,
    )
