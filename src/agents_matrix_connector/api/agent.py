"""The agent listener (design D3): `/chat/v1/*`, `/healthz` and `/readyz` on its own port.

The orchestrator calls it with `Authorization: Bearer <token>`. Only the sha256 hex of the
token is configured (`MATRIX_AGENT_TOKEN_SHA256`) and it is compared in constant time, like the
orchestrator's `api/service_auth.py:ServiceGate`. The token is the only credential: no source
allowlist applies, because the orchestrator pod is outside `MATRIX_API_ALLOWED_CIDRS`. The
token and its hash never reach a log line or an error body. Probes stay open.

The `/chat/v1` routes (design D2, D4, D5, D6) act for the session named by `X-Orch-Session`.
They list the rooms the session may post to, queue messages and reactions as `sends` rows and
read a send's status. One predicate, `may_post`, decides every room for the send routes and the
listing. The api only inserts `sends` rows and never writes `room_bindings`: the bot's claim of
a send re-checks the room and is the binding step (D9).
"""

import hashlib
import hmac
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Header
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_matrix_connector import policy
from agents_matrix_connector.api.app import (
    EXEMPT_PATHS,
    add_error_handlers,
    add_probes,
    error_response,
)
from agents_matrix_connector.api.rooms import agent_room_decision
from agents_matrix_connector.api.schemas import ApiError
from agents_matrix_connector.chunks import split
from agents_matrix_connector.db import bindings, directory, rules, sends
from agents_matrix_connector.settings import ApiSettings, ConnectorSettings

log = logging.getLogger(__name__)

CHAT_PREFIX = "/chat/v1"
HEALTH_METHODS = frozenset({"GET", "HEAD"})

MAX_TEXT_CHARS = 16000
MAX_KEY_CODE_POINTS = 16
DUPLICATE_SECONDS = 60
RATE_WINDOW_SECONDS = 60
ROOM_WINDOW_SECONDS = 3600
SLUG_MAX = 40
_SLUG_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", re.ASCII)
_UNUSABLE_REFERENCE = "not a message id (m<n> or s<n>) this session can reference"

Verdict = Literal["ok", "room_not_allowed", "room_bound", "room_not_authorised", "session_bound"]
_REFUSALS: dict[str, tuple[int, str]] = {
    "room_not_allowed": (403, "room: not allowed for agent sends"),
    "room_bound": (409, "room: bound to another session"),
    "room_not_authorised": (
        403,
        "room: not bound to this session and agents may not start a chat there",
    ),
    "session_bound": (409, "room: this session is already bound to another room"),
}


def _bearer(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            token = token.strip()
            return token if scheme.lower() == "bearer" and token else None
    return None


class AgentGate:
    """401 unless the bearer token's sha256 hex equals `token_sha256`; probes are exempt."""

    def __init__(self, app: ASGIApp, token_sha256: str):
        self.app = app
        self.digest = token_sha256.encode()

    def authenticate(self, token: str) -> bool:
        presented = hashlib.sha256(token.encode()).hexdigest().encode()
        return hmac.compare_digest(presented, self.digest)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope.get("path", "")
        if path in EXEMPT_PATHS and method in HEALTH_METHODS:
            await self.app(scope, receive, send)
            return
        token = _bearer(scope)
        if token is None or not self.authenticate(token):
            log.info("agent request method=%s path=%r status=401", method, path)
            response = error_response(
                401,
                "unauthorized",
                "a valid agent token is required",
                {"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def valid_key(key: str) -> bool:
    """A reaction key: 1 to 16 code points, none ASCII, whitespace or a control character."""
    return 1 <= len(key) <= MAX_KEY_CODE_POINTS and all(
        ord(ch) > 0x7F and not ch.isspace() and unicodedata.category(ch) != "Cc" for ch in key
    )


async def may_post(
    conn: psycopg.AsyncConnection,
    session_id: uuid.UUID,
    room: directory.RoomRow | None,
    rule_list: list[policy.Rule],
    settings: ConnectorSettings,
) -> Verdict:
    """Whether the session may post to the room (design D4); None is an unknown room.

    (a) the room passes `agent_room_decision`; (b) it is bound to the session, or it is
    unbound, flagged `agents_may_initiate` and the session is bound to no room. A reservation by
    another session's first run counts as bound to another session. The refusals of (b) come
    in the order room_bound, room_not_authorised, session_bound.
    """
    if room is None or not agent_room_decision(room, rule_list, settings).allowed:
        return "room_not_allowed"
    binding = await bindings.active_for_room(conn, room.room_id)
    if binding is not None:
        return "ok" if binding.session_id == session_id else "room_bound"
    setting = await rules.get_room_settings(conn, room.room_id)
    if setting is None or not setting.agents_may_initiate:
        return "room_not_authorised"
    if await bindings.active_for_session(conn, session_id) is not None:
        return "session_bound"
    return "ok"


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageIn(_Body):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    room: str | None = None
    """A Matrix room id; omitted, the room of `reply_to`, else the session's bound room."""
    reply_to: str | None = None
    """`m<n>` (a chat message) or `s<n>` (a sent message of this session)."""


class ReactionIn(_Body):
    to: str
    """`m<n>` or `s<n>`; the reaction goes to that message's room."""
    key: str


class SendAccepted(BaseModel):
    id: str
    status: Literal["queued", "sending", "sent"]


class ChatRoom(BaseModel):
    room_id: str
    name: str | None
    kind: str | None
    bound: bool
    agents_may_initiate: bool


class ChatRooms(BaseModel):
    rooms: list[ChatRoom]


class SendStatus(BaseModel):
    id: str
    kind: Literal["message", "reaction"]
    status: Literal["queued", "sending", "sent", "failed", "refused"]
    reason: str | None
    room_id: str
    created_at: datetime
    sent_at: datetime | None


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorBody(BaseModel):
    error: ErrorDetail


def _errors(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {status: {"model": ErrorBody} for status in statuses}


@dataclass(frozen=True)
class Caller:
    session_id: uuid.UUID
    slug: str | None


def caller(
    session: Annotated[
        str | None,
        Header(
            alias="X-Orch-Session",
            description="the calling session's uuid; missing or malformed is 401 unauthorized",
        ),
    ] = None,
    slug: Annotated[
        str | None, Header(alias="X-Orch-Slug", description="the calling session's slug")
    ] = None,
) -> Caller:
    """The session the orchestrator calls for; a missing or malformed uuid is 401 and an
    invalid slug 400."""
    try:
        session_id = uuid.UUID(session or "")
    except ValueError:
        raise ApiError(401, "unauthorized", "X-Orch-Session: a session uuid is required") from None
    if slug and not valid_slug(slug):
        raise ApiError(
            400,
            "invalid_slug",
            f"X-Orch-Slug: a session slug (lowercase letters, digits and hyphens, at most "
            f"{SLUG_MAX} characters, not orchestrator or orchestrator-*) is required when sent",
        )
    return Caller(session_id, slug or None)


def valid_slug(slug: str) -> bool:
    """The orchestrator's session slug rules: a DNS-1123 label of at most 40 characters that is
    not `orchestrator` or `orchestrator-*` (`agents_orchestrator.spec.validate_slug`)."""
    return (
        len(slug) <= SLUG_MAX
        and _SLUG_RE.fullmatch(slug) is not None
        and slug != "orchestrator"
        and not slug.startswith("orchestrator-")
    )


def _bad_reference(field: str, why: str) -> ApiError:
    return ApiError(422, "bad_reference", f"{field}: {why}")


def _chunk_count(kind: str, text: str | None, chunk_chars: int) -> int:
    """Matrix events one send puts into its room: a reaction is one, a message one per chunk."""
    return 1 if kind == "reaction" else max(1, len(split(text or "", chunk_chars)))


async def _room_chunks_last_hour(
    conn: psycopg.AsyncConnection, room_id: str, chunk_chars: int
) -> int:
    """Chunks sent or queued to the room in the last hour, by any session; a refused send
    delivered nothing and does not count."""
    cur = await conn.execute(
        "SELECT kind, text FROM sends WHERE room_id = %s AND status <> 'refused'"
        " AND created_at > now() - make_interval(secs => %s)",
        (room_id, ROOM_WINDOW_SECONDS),
    )
    return sum(_chunk_count(kind, text, chunk_chars) for kind, text in await cur.fetchall())


async def _lock(conn: psycopg.AsyncConnection, kind: str, key: str) -> None:
    """Hold a transaction-scoped advisory lock on `kind:key` until the transaction ends."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"agents-matrix-connector:sends:{kind}:{key}",),
    )


async def _resolve(
    conn: psycopg.AsyncConnection, who: Caller, field: str, ref: str, room: str | None
) -> sends.Ref:
    """An `m<n>` or `s<n>` this session may reference (design D6), else 422 bad_reference.

    Unknown ids, another session's sends and another room's ids share one message.
    """
    resolved = await sends.resolve(conn, ref)
    if resolved is None or (resolved.source == "send" and resolved.session_id != who.session_id):
        raise _bad_reference(field, _UNUSABLE_REFERENCE)
    if room is not None and resolved.room_id != room:
        raise _bad_reference(field, _UNUSABLE_REFERENCE)
    if resolved.kind == "reaction":
        raise _bad_reference(field, "a reaction cannot be replied or reacted to")
    if resolved.source == "send" and (resolved.status != "sent" or resolved.event_id is None):
        raise _bad_reference(field, "the message is not sent yet")
    return resolved


def build_chat_router(pool: AsyncConnectionPool, settings: ApiSettings) -> APIRouter:
    router = APIRouter(prefix=CHAT_PREFIX)
    send_errors = _errors(400, 401, 403, 409, 422, 429, 503)

    async def verdict(conn: psycopg.AsyncConnection, who: Caller, room_id: str) -> Verdict:
        room = await directory.get_room(pool, room_id)
        return await may_post(conn, who.session_id, room, await rules.list_rules(conn), settings)

    async def queue(
        conn: psycopg.AsyncConnection,
        who: Caller,
        *,
        kind: str,
        room_id: str,
        text: str | None,
        key: str | None,
        reply_to: str | None,
        ref_event_id: str | None,
        masked_field: str | None = None,
    ) -> SendAccepted:
        """The room checks, then, under the session's and the room's advisory locks, the
        duplicate, the per-session rate limit and the per-room hourly chunk cap; then the insert.

        The room checks are advisory: the bot's claim re-checks the room (design D5, D9). The
        locks make the two limits atomic with the insert, so parallel requests cannot exceed
        them. The session lock is always taken before the room lock, so two requests never
        wait on each other in a cycle. A room refusal for an inbound message of a room that is
        not the session's own answers `bad_reference` on `masked_field` like an unknown id, so
        a session cannot probe which ids exist in other rooms.
        """
        outcome = await verdict(conn, who, room_id)
        if outcome != "ok":
            log.info("agent send refused session=%s code=%s", who.session_id, outcome)
            if masked_field is not None:
                own = await bindings.active_for_session(conn, who.session_id)
                if own is None or own.room_id != room_id:
                    raise _bad_reference(masked_field, _UNUSABLE_REFERENCE)
            status, message = _REFUSALS[outcome]
            raise ApiError(status, outcome, message)
        async with conn.transaction():
            await _lock(conn, "session", str(who.session_id))
            await _lock(conn, "room", room_id)
            duplicate = await sends.find_duplicate(
                conn,
                session_id=who.session_id,
                room_id=room_id,
                kind=kind,
                text=text,
                key=key,
                reply_to=reply_to,
                seconds=DUPLICATE_SECONDS,
            )
            if duplicate is not None:
                log.info("agent send duplicate session=%s id=s%s", who.session_id, duplicate.id)
                return SendAccepted(id=f"s{duplicate.id}", status=duplicate.status)
            recent = await sends.count_recent(conn, who.session_id, RATE_WINDOW_SECONDS)
            if recent >= settings.agent_rate_per_minute:
                log.info("agent send refused session=%s code=rate_limited", who.session_id)
                raise ApiError(
                    429,
                    "rate_limited",
                    f"at most {settings.agent_rate_per_minute} sends per session in 60 s",
                )
            cap = settings.agent_room_chunks_per_hour
            chunks = _chunk_count(kind, text, settings.reply_chunk_chars)
            if (
                await _room_chunks_last_hour(conn, room_id, settings.reply_chunk_chars) + chunks
                > cap
            ):
                log.info("agent send refused session=%s code=room_chunk_limit", who.session_id)
                raise ApiError(
                    429, "room_chunk_limit", f"room: at most {cap} chunks per hour to one room"
                )
            send_id = await sends.insert(
                conn,
                session_id=who.session_id,
                slug=who.slug,
                kind=kind,
                room_id=room_id,
                text=text,
                key=key,
                reply_to=reply_to,
                ref_event_id=ref_event_id,
            )
        log.info("agent send queued session=%s id=s%s kind=%s", who.session_id, send_id, kind)
        return SendAccepted(id=f"s{send_id}", status="queued")

    @router.get("/rooms", response_model=ChatRooms, responses=_errors(400, 401, 503))
    async def list_rooms(who: Annotated[Caller, Depends(caller)]) -> ChatRooms:
        """The rooms this session may post to: its bound room, else the unbound flagged rooms."""
        found = []
        async with pool.connection() as conn:
            rule_list = await rules.list_rules(conn)
            own = await bindings.active_for_session(conn, who.session_id)
            for listed in await directory.list_rooms(pool):
                room = await directory.get_room(pool, listed.room_id)
                if room is None:  # forgotten since the listing
                    continue
                if await may_post(conn, who.session_id, room, rule_list, settings) != "ok":
                    continue
                setting = await rules.get_room_settings(conn, room.room_id)
                found.append(
                    ChatRoom(
                        room_id=room.room_id,
                        name=room.name,
                        kind=room.kind,
                        bound=own is not None and own.room_id == room.room_id,
                        agents_may_initiate=setting is not None and setting.agents_may_initiate,
                    )
                )
        found.sort(key=lambda r: (not r.bound, r.name is None, r.name or "", r.room_id))
        return ChatRooms(rooms=found)

    @router.post("/messages", status_code=202, response_model=SendAccepted, responses=send_errors)
    async def send_message(
        who: Annotated[Caller, Depends(caller)], body: MessageIn
    ) -> SendAccepted:
        """Queue a message, optionally a quoted reply."""
        if not body.text.strip():
            raise ApiError(
                422, "invalid_text", "text: needs a non-whitespace character to be delivered"
            )
        async with pool.connection() as conn:
            ref = None
            if body.reply_to is not None:
                ref = await _resolve(conn, who, "reply_to", body.reply_to, body.room)
            if body.room is not None:
                room_id = body.room
            elif ref is not None:
                room_id = ref.room_id
            else:
                own = await bindings.active_for_session(conn, who.session_id)
                if own is None:
                    raise ApiError(
                        422, "room_required", "room: required, this session is bound to no room"
                    )
                room_id = own.room_id
            return await queue(
                conn,
                who,
                kind="message",
                room_id=room_id,
                text=body.text,
                key=None,
                reply_to=body.reply_to,
                ref_event_id=ref.event_id if ref else None,
                masked_field="reply_to" if ref is not None and ref.source == "inbound" else None,
            )

    @router.post("/reactions", status_code=202, response_model=SendAccepted, responses=send_errors)
    async def send_reaction(
        who: Annotated[Caller, Depends(caller)], body: ReactionIn
    ) -> SendAccepted:
        """Queue a reaction to a message; it goes to that message's room."""
        if not valid_key(body.key):
            raise ApiError(
                422,
                "invalid_key",
                "key: 1 to 16 code points, no ASCII, whitespace or control characters",
            )
        async with pool.connection() as conn:
            ref = await _resolve(conn, who, "to", body.to, None)
            return await queue(
                conn,
                who,
                kind="reaction",
                room_id=ref.room_id,
                text=None,
                key=body.key,
                reply_to=body.to,
                ref_event_id=ref.event_id,
                masked_field="to" if ref.source == "inbound" else None,
            )

    @router.get("/messages/{id}", response_model=SendStatus, responses=_errors(400, 401, 404, 503))
    async def send_status(who: Annotated[Caller, Depends(caller)], id: str) -> SendStatus:
        """One of this session's sends; any other id is 404."""
        parsed = sends.parse_ref(id)
        row = None
        if parsed is not None and parsed[0] == "s":
            async with pool.connection() as conn:
                row = await sends.get(conn, parsed[1])
        if row is None or row.session_id != who.session_id:
            raise ApiError(404, "not_found", f"no send {id!r} of this session")
        return SendStatus(
            id=f"s{row.id}",
            kind=row.kind,
            status=row.status,
            reason=row.reason,
            room_id=row.room_id,
            created_at=row.created_at,
            sent_at=row.sent_at,
        )

    return router


def create_agent_app(pool: AsyncConnectionPool, settings: ApiSettings) -> FastAPI:
    app = FastAPI(
        title="agents-matrix-connector agent API",
        openapi_url=f"{CHAT_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(build_chat_router(pool, settings))
    app.add_middleware(AgentGate, token_sha256=settings.agent_token_sha256.get_secret_value())
    add_error_handlers(app)
    add_probes(app, pool)
    return app
