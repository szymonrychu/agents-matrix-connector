"""Room requests on the agent listener (open a DM or invite a user by id, link or name).

Admin routes (relayed 1:1 by the orchestrator to superleaders): `POST /room-requests` inserts a
row and `GET /room-requests/{request_id}` reads it. Internal routes (orchestrator only, never
relayed): `PUT /room-requests/{request_id}/decision` attaches the taskboard decision the
orchestrator asked the user, and `POST /room-requests/{request_id}/confirm` records the user's
answer. Only that confirm, with the attached decision id, makes a row `confirmed`, so the bot
never opens or invites without the user's yes. The api never talks to Matrix or the bridge.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from agents_matrix_connector import discovery
from agents_matrix_connector.api.schemas import ApiError
from agents_matrix_connector.db import directory
from agents_matrix_connector.db import room_requests as repo
from agents_matrix_connector.settings import ApiSettings
from agents_matrix_connector.targets import InvalidTarget, Target, VanityLink, parse_target

log = logging.getLogger(__name__)

CAP_WINDOW_SECONDS = 3600
TASK_KEY_PATTERN = r"^T-[0-9]+$"
VANITY_MESSAGE = (
    "facebook.com/<username> links cannot be resolved; pass the person's name or a "
    "profile.php?id= link"
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoomRequestIn(_Body):
    target: str = Field(min_length=1, max_length=500)
    """A Matrix user id, a Facebook id, a profile.php?id= link or a display name."""
    action: Literal["open", "invite"] = "open"
    room_id: str | None = None
    """The joined Matrix room to invite into (invite only)."""
    task_key: str = Field(pattern=TASK_KEY_PATTERN)
    """The taskboard task the confirmation decision is asked on."""


class RoomRequestView(BaseModel):
    id: int
    action: Literal["open", "invite", "accept"]
    status: Literal[
        "resolving",
        "candidates",
        "pending_confirmation",
        "confirmed",
        "working",
        "done",
        "failed",
        "rejected",
    ]
    target_kind: Literal["matrix", "facebook", "name"] | None
    target: str | None
    display_name: str | None
    room_id: str | None
    candidates: list[dict[str, Any]] | None
    task_key: str | None
    decision_id: int | None
    reason: str | None
    created_at: datetime
    updated_at: datetime


class DecisionIn(_Body):
    task_key: str = Field(pattern=TASK_KEY_PATTERN)
    decision_id: int = Field(ge=1)


class ConfirmIn(_Body):
    decision_id: int = Field(ge=1)
    approved: bool


def view(row: repo.RoomRequest) -> RoomRequestView:
    return RoomRequestView(
        id=row.id,
        action=row.action,
        status=row.status,
        target_kind=row.target_kind,
        target=row.target,
        display_name=row.display_name,
        room_id=row.room_id,
        candidates=row.candidates,
        task_key=row.task_key,
        decision_id=row.decision_id,
        reason=row.reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _session_uuid(raw: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if raw else None
    except ValueError:
        return None


def _not_found(request_id: int) -> ApiError:
    return ApiError(404, "not_found", f"room request {request_id} not found")


def _not_pending(row: repo.RoomRequest) -> ApiError:
    return ApiError(
        409,
        "not_pending",
        f"room request {row.id} is {row.status}, not pending_confirmation with this decision",
    )


def _invite_not_possible(why: str) -> ApiError:
    return ApiError(422, "invite_not_possible", why)


def build_room_requests_router(pool: AsyncConnectionPool, settings: ApiSettings) -> APIRouter:
    router = APIRouter()
    refused = frozenset(settings.self_ids() | set(settings.bridge_bots))
    bridge_server = settings.user_id.split(":", 1)[1]
    puppet = re.compile(settings.puppet_pattern)

    def parse(text: str) -> Target:
        try:
            return parse_target(
                text,
                puppet_pattern=settings.puppet_pattern,
                bridge_server=bridge_server,
                refused=refused,
            )
        except VanityLink:
            raise ApiError(422, "vanity_link_unresolvable", VANITY_MESSAGE) from None
        except InvalidTarget as exc:
            raise ApiError(422, "invalid_target", f"target: {exc}") from None

    async def check_invite(body: RoomRequestIn, target: Target) -> None:
        if body.room_id is None:
            raise _invite_not_possible("room_id: required to invite")
        room = await directory.get_room(pool, body.room_id)
        if room is None or room.membership != "join" or room.kind != "matrix":
            raise _invite_not_possible("room_id: not a joined Matrix room")
        if target.kind != "matrix":
            raise _invite_not_possible("target: only a Matrix user id can be invited")

    async def known_name(conn, target: Target) -> str | None:
        if target.kind == "facebook":
            cur = await conn.execute(
                "SELECT display_name FROM users WHERE facebook_id = %s"
                " AND display_name IS NOT NULL ORDER BY last_seen_at DESC NULLS LAST LIMIT 1",
                (target.value,),
            )
        else:
            cur = await conn.execute(
                "SELECT display_name FROM users WHERE user_id = %s", (target.value,)
            )
        row = await cur.fetchone()
        return row[0] if row else None

    @router.post("/room-requests", status_code=201, response_model=RoomRequestView)
    async def create(
        body: RoomRequestIn,
        session: Annotated[str | None, Header(alias="X-Orch-Session")] = None,
        slug: Annotated[str | None, Header(alias="X-Orch-Slug")] = None,
    ) -> RoomRequestView:
        """Queue an open or invite. Nothing happens on Matrix or Facebook until the user's yes
        on the taskboard decision is confirmed through the internal confirm route."""
        target = parse(body.target)
        if body.action == "invite":
            await check_invite(body, target)
        elif body.room_id is not None:
            raise ApiError(422, "invalid_request", "room_id: only for action invite")
        async with pool.connection() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("agents-matrix-connector:room-requests:cap",),
            )
            recent = await repo.count_recent_opens(conn, CAP_WINDOW_SECONDS)
            if recent >= settings.room_requests_per_hour:
                log.info("room request refused code=rate_limited slug=%s", slug)
                raise ApiError(
                    429,
                    "rate_limited",
                    f"at most {settings.room_requests_per_hour} room requests per hour",
                )
            fields: dict[str, Any] = {}
            if target.kind == "name":
                matches = await directory.find_users_by_name(conn, target.value, refused)
                if len(matches) == 1:
                    user = matches[0]
                    fb = user.facebook_id or discovery.facebook_id(
                        user.user_id, puppet, bridge_server
                    )
                    fields = {
                        "status": "pending_confirmation",
                        "target_kind": "facebook" if fb else "matrix",
                        "target": fb or user.user_id,
                        "display_name": user.display_name,
                    }
                elif matches:
                    fields = {
                        "status": "candidates",
                        "target_kind": "name",
                        "target": target.value,
                        "candidates": [
                            {
                                "user_id": u.user_id,
                                "display_name": u.display_name,
                                "facebook_id": u.facebook_id,
                            }
                            for u in matches
                        ],
                    }
                else:
                    fields = {"status": "resolving", "target_kind": "name", "target": target.value}
            else:
                fields = {
                    "status": "pending_confirmation",
                    "target_kind": target.kind,
                    "target": target.value,
                    "display_name": await known_name(conn, target),
                }
            row = await repo.insert(
                conn,
                action=body.action,
                room_id=body.room_id,
                task_key=body.task_key,
                requested_by=_session_uuid(session),
                requested_slug=slug,
                **fields,
            )
        log.info(
            "room request created id=%s action=%s status=%s kind=%s slug=%s",
            row.id,
            row.action,
            row.status,
            row.target_kind,
            slug,
        )
        return view(row)

    @router.get("/room-requests/{request_id}", response_model=RoomRequestView)
    async def get(request_id: int) -> RoomRequestView:
        async with pool.connection() as conn:
            row = await repo.get(conn, request_id)
        if row is None:
            raise _not_found(request_id)
        return view(row)

    return router


def build_internal_router(pool: AsyncConnectionPool) -> APIRouter:
    router = APIRouter()

    @router.put("/room-requests/{request_id}/decision", response_model=RoomRequestView)
    async def attach_decision(request_id: int, body: DecisionIn) -> RoomRequestView:
        """Attach the taskboard decision the orchestrator asked; once, and only while the row
        waits for confirmation."""
        async with pool.connection() as conn, conn.transaction():
            row = await repo.get(conn, request_id)
            if row is None:
                raise _not_found(request_id)
            if not await repo.attach_decision(conn, request_id, body.task_key, body.decision_id):
                raise _not_pending(row)
            row = await repo.get(conn, request_id)
        log.info("room request decision id=%s decision=%s", request_id, body.decision_id)
        return view(row)

    @router.post("/room-requests/{request_id}/confirm", response_model=RoomRequestView)
    async def confirm(request_id: int, body: ConfirmIn) -> RoomRequestView:
        """The user's answer: confirmed (approved) or rejected. Only with the attached
        decision id."""
        async with pool.connection() as conn, conn.transaction():
            row = await repo.get(conn, request_id)
            if row is None:
                raise _not_found(request_id)
            if not await repo.confirm(conn, request_id, body.decision_id, body.approved):
                raise _not_pending(row)
            row = await repo.get(conn, request_id)
        log.info("room request answered id=%s status=%s", request_id, row.status)
        return view(row)

    return router
