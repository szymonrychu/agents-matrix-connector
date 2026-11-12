"""The agent listener (design D3): `/chat/v1/*`, `/healthz` and `/readyz` on its own port.

The orchestrator calls it with `Authorization: Bearer <token>`. Only the sha256 hex of the
token is configured (`MATRIX_AGENT_TOKEN_SHA256`) and it is compared in constant time, like the
orchestrator's `api/service_auth.py:ServiceGate`. The token is the only credential: no source
allowlist applies, because the orchestrator pod is outside `MATRIX_API_ALLOWED_CIDRS`. The
token and its hash never reach a log line or an error body. Probes stay open.

The `/chat/v1` routes (design D2, D4, D5, D6) act for the session named by `X-Orch-Session`.
They list the rooms the session may post to, queue messages and reactions as `sends` rows and
read a send's status. One predicate, `may_post`, decides every room for the send routes and the
listing. The api inserts `sends` rows and writes `room_notes` and `room_skills`, but never
`room_bindings`: the bot's claim of a send re-checks the room and is the binding step (D9).
The notes and skills routes never take a room: it is the caller session's active binding.
"""

import asyncio
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
from agents_common.orchestrator import OrchestratorError, OrchestratorUnavailable
from fastapi import APIRouter, Depends, FastAPI, Header
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from agents_matrix_connector import anonymize, discovery, media, policy
from agents_matrix_connector.api.app import (
    EXEMPT_PATHS,
    add_error_handlers,
    add_probes,
    error_response,
)
from agents_matrix_connector.api.room_requests import (
    build_internal_router,
    build_room_requests_router,
)
from agents_matrix_connector.api.rooms import agent_room_decision, build_rooms_router
from agents_matrix_connector.api.rules import build_rules_router
from agents_matrix_connector.api.schemas import ApiError
from agents_matrix_connector.chunks import split
from agents_matrix_connector.db import (
    bindings,
    directory,
    notes,
    rules,
    send_files,
    sends,
    skills,
)
from agents_matrix_connector.prompt import memory_rule
from agents_matrix_connector.settings import ApiSettings, ConnectorSettings

log = logging.getLogger(__name__)

CHAT_PREFIX = "/chat/v1"
ADMIN_PREFIX = CHAT_PREFIX + "/admin"
INTERNAL_PREFIX = CHAT_PREFIX + "/internal"
"""Routes only the orchestrator calls (never relayed to agents): room request confirmation."""
_ADMIN_ROUTES = frozenset(
    {
        ("/rooms", "GET"),
        ("/rooms/{room_id}", "GET"),
        ("/rooms/{room_id}/allow", "POST"),
        ("/rooms/{room_id}/settings", "PUT"),
        ("/users", "GET"),
        ("/rules", "GET"),
        ("/rules", "POST"),
        ("/rules/{rule_id}", "DELETE"),
        ("/room-requests", "POST"),
        ("/room-requests/{request_id}", "GET"),
    }
)
HEALTH_METHODS = frozenset({"GET", "HEAD"})

MAX_TEXT_CHARS = 16000
MAX_FILES_PER_SEND = 4
"""The schema cap of `files`; `MATRIX_AGENT_FILES_PER_SEND` may set a lower one."""
MAX_KEY_CODE_POINTS = 16
DUPLICATE_SECONDS = 60
RATE_WINDOW_SECONDS = 60
ROOM_WINDOW_SECONDS = 3600
SLUG_MAX = 40
SKILL_DESCRIPTION_MAX = 1024
_SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}", re.ASCII)
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


async def relay_occupier(conn: psycopg.AsyncConnection, room_id: str) -> uuid.UUID | None:
    """The session bound to a room agents may start a chat in, else None.

    Named on `room_bound` only for the orchestrator (the sole caller), which relays the post
    through it. A reservation has no session yet; an unflagged room is not disclosed.
    """
    setting = await rules.get_room_settings(conn, room_id)
    if setting is None or not setting.agents_may_initiate:
        return None
    binding = await bindings.active_for_room(conn, room_id)
    return binding.session_id if binding is not None else None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageIn(_Body):
    text: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_TEXT_CHARS,
        description="Needs a non-whitespace character unless files are given (else 422 "
        "invalid_text).",
    )
    files: list[str] = Field(
        default_factory=list,
        max_length=MAX_FILES_PER_SEND,
        description="Relative paths of files in the calling agent's workdir, sent after the "
        "text, one Matrix event each. Needs the room's agents_may_send_files and the "
        "X-Orch-Agent header. Images at most 8000000 bytes, other files 25000000 "
        "(MATRIX_AGENT_IMAGE_MAX_BYTES, MATRIX_AGENT_FILE_MAX_BYTES).",
    )
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
    agents_may_send_files: bool


class ChatRooms(BaseModel):
    rooms: list[ChatRoom]


class SentFile(BaseModel):
    name: str
    mime: str
    size: int
    status: Literal["queued", "uploaded", "sent", "failed"]


class SendStatus(BaseModel):
    id: str
    kind: Literal["message", "reaction"]
    status: Literal["queued", "sending", "sent", "failed", "refused"]
    reason: str | None
    room_id: str
    created_at: datetime
    sent_at: datetime | None
    files: list[SentFile]
    """The message's files in order; empty for a reaction or a message without files."""


class NotesIn(_Body):
    body: str
    """The whole notes body; empty forgets the notes."""
    expected_version: int = Field(ge=0)
    """The version read before (0 when none exists yet); a stale one is 409 version_conflict."""


class RoomNotes(BaseModel):
    room_id: str
    body: str
    version: int
    updated_at: datetime | None
    chars: int
    max_chars: int
    rule: str
    """The room memory rule: how to keep notes and skills."""


class SkillIn(_Body):
    description: str = Field(min_length=1, max_length=SKILL_DESCRIPTION_MAX)
    """When to use the skill (becomes the SKILL.md description)."""
    body: str = Field(min_length=1)
    """The skill's steps with their inputs (becomes the SKILL.md content)."""
    expected_version: int = Field(ge=0)
    """The version read before (0 to create); a stale one is 409 version_conflict."""


class SkillMeta(BaseModel):
    name: str
    description: str
    version: int
    updated_at: datetime
    chars: int


class RoomSkills(BaseModel):
    room_id: str
    skills: list[SkillMeta]
    max_skills: int
    max_chars: int


class RoomSkill(BaseModel):
    room_id: str
    name: str
    description: str
    body: str
    version: int
    updated_at: datetime
    chars: int
    max_chars: int


class SkillDeleted(BaseModel):
    room_id: str
    name: str
    deleted: bool
    """Always true on 200."""


class ErrorDetail(BaseModel):
    code: str
    message: str
    occupier: uuid.UUID | None = Field(
        default=None,
        description="Only on 409 room_bound, for the orchestrator: the session bound to a room "
        "agents may start a chat in.",
    )


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


_ROOM_UNBOUND = "this session is not bound to a chat room"


async def _own_room(conn: psycopg.AsyncConnection, who: Caller) -> str:
    """The room of the caller session's active binding; never a request parameter."""
    own = await bindings.active_for_session(conn, who.session_id)
    if own is None:
        raise ApiError(409, "room_unbound", _ROOM_UNBOUND)
    return own.room_id


def _version_conflict(expected: int, current: int) -> ApiError:
    return ApiError(
        409,
        "version_conflict",
        f"changed since version {expected}; current version is {current}: re-read and merge",
    )


def _skill_name(name: str) -> str:
    """A valid skill name (a SKILL.md directory name), else 422."""
    if _SKILL_NAME_RE.fullmatch(name) is None:
        raise ApiError(
            422,
            "invalid_request",
            "name: lowercase letters, digits and hyphens, 1 to 64 characters, starting with a "
            "letter or digit",
        )
    return name


def _skill_not_found() -> ApiError:
    return ApiError(404, "skill_not_found", "no skill with that name in this room")


def _too_large(field: str, cap: int) -> ApiError:
    return ApiError(413, "file_too_large", f"{field}: larger than {cap} bytes")


def _bad_reference(field: str, why: str) -> ApiError:
    return ApiError(422, "bad_reference", f"{field}: {why}")


def _chunk_count(kind: str, text: str | None, chunk_chars: int, files: int = 0) -> int:
    """Matrix events one send puts into its room: a reaction is one, a message one per text
    chunk plus one per file (a files-only message has no text chunk)."""
    if kind == "reaction":
        return 1
    return max(1, len(split(text or "", chunk_chars)) + files)


async def _room_chunks_last_hour(
    conn: psycopg.AsyncConnection, room_id: str, chunk_chars: int
) -> int:
    """Chunks sent or queued to the room in the last hour, by any session; a refused send
    delivered nothing and does not count."""
    cur = await conn.execute(
        "SELECT kind, text, (SELECT count(*) FROM send_files f WHERE f.send_id = sends.id)"
        " FROM sends WHERE room_id = %s AND status <> 'refused'"
        " AND created_at > now() - make_interval(secs => %s)",
        (room_id, ROOM_WINDOW_SECONDS),
    )
    return sum(
        _chunk_count(kind, text, chunk_chars, files) for kind, text, files in await cur.fetchall()
    )


def _file_status(send: sends.SendRow, file: send_files.SendFile, delivered: str | None) -> str:
    """A file's delivery: its outbound row's end, else uploaded once the homeserver has it."""
    if delivered == "sent":
        return "sent"
    if delivered == "failed" or (delivered is None and send.status in ("failed", "refused")):
        return "failed"
    return "uploaded" if file.content_uri is not None else "queued"


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


def build_chat_router(
    pool: AsyncConnectionPool, orchestrator: Any, settings: ApiSettings
) -> APIRouter:
    """`orchestrator` is the api's `OrchestratorClient`: the send route downloads an agent's
    files through it."""
    router = APIRouter(prefix=CHAT_PREFIX)
    send_errors = _errors(400, 401, 403, 404, 409, 413, 422, 429, 503)
    puppet = re.compile(settings.puppet_pattern)
    bridge_server = settings.user_id.split(":", 1)[1]
    excluded = settings.self_ids() | set(settings.bridge_bots)
    labeler = anonymize.Labeler(
        settings.anonymize_admins,
        lambda user_id: discovery.facebook_id(user_id, puppet, bridge_server),
    )

    async def queue(
        who: Caller,
        *,
        kind: str,
        room_id: str,
        text: str | None,
        key: str | None,
        reply_to: str | None,
        ref_event_id: str | None,
        masked_field: str | None = None,
        files: list[str] | None = None,
        agent: str | None = None,
    ) -> SendAccepted:
        """The room checks, then, under the session's and the room's advisory locks, the
        duplicate, the per-session rate limit and the per-room hourly chunk cap; then the insert.

        With `files`: after the room checks, the room's `agents_may_send_files`, the file names
        in an anonymized room, the path rules and a pre-check of the rate and room byte limits;
        then each file is downloaded through the orchestrator from the agent `agent` with no
        pool connection held, and the locked transaction re-checks everything, the room byte
        cap included, before it inserts the send with its `send_files` rows.

        A request holds at most one pool connection at a time: the room is read before this
        request takes its connection, and the callers release theirs before calling.

        The room checks are advisory: the bot's claim re-checks the room (design D5, D9). The
        locks make the two limits atomic with the insert, so parallel requests cannot exceed
        them. The session lock is always taken before the room lock, so two requests never
        wait on each other in a cycle. A room refusal for an inbound message of a room that is
        not the session's own answers `bad_reference` on `masked_field` like an unknown id, so
        a session cannot probe which ids exist in other rooms.
        """
        room = await directory.get_room(pool, room_id)
        async with pool.connection() as conn:
            outcome = await may_post(
                conn, who.session_id, room, await rules.list_rules(conn), settings
            )
            if outcome != "ok":
                log.info("agent send refused session=%s code=%s", who.session_id, outcome)
                if masked_field is not None:
                    own = await bindings.active_for_session(conn, who.session_id)
                    if own is None or own.room_id != room_id:
                        raise _bad_reference(masked_field, _UNUSABLE_REFERENCE)
                status, message = _REFUSALS[outcome]
                occupier = await relay_occupier(conn, room_id) if outcome == "room_bound" else None
                raise ApiError(
                    status, outcome, message, {"occupier": str(occupier)} if occupier else None
                )
            setting = await rules.get_room_settings(conn, room_id)
            paths = files or []
            if paths and (setting is None or not setting.agents_may_send_files):
                log.info("agent send refused session=%s code=files_not_allowed", who.session_id)
                raise ApiError(
                    403,
                    "files_not_allowed",
                    "files: this room does not accept files from agents (agents_may_send_files)",
                )
            if setting is not None and setting.anonymize_names:
                named = [("text", text)] if text is not None else []
                named += [(f"files[{n}]", media.safe_name(p)) for n, p in enumerate(paths)]
                for field, value in named:
                    found = anonymize.pseudonyms_in(value)
                    if found:
                        log.info(
                            "agent send refused session=%s code=%s",
                            who.session_id,
                            anonymize.PSEUDONYM_DISCLOSED,
                        )
                        raise ApiError(
                            422,
                            anonymize.PSEUDONYM_DISCLOSED,
                            f"{field}: " + anonymize.disclosure_message(found),
                        )
            for n, path in enumerate(paths):
                try:
                    media.check_agent_path(path)
                except ValueError:
                    log.info("agent send refused session=%s code=invalid_file_path", who.session_id)
                    raise ApiError(
                        422,
                        "invalid_file_path",
                        f"files[{n}]: a relative workdir path without '..', hidden components"
                        " (except a leading .attachments/) or key and env file names",
                    ) from None
            if paths:
                await _precheck(conn, who, room_id)
        new_files = await _fetch_all(who, agent, paths) if paths else []
        async with pool.connection() as conn:
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
                    file_shas=[f.sha256 for f in new_files],
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
                chunks = _chunk_count(kind, text, settings.reply_chunk_chars, len(new_files))
                if (
                    await _room_chunks_last_hour(conn, room_id, settings.reply_chunk_chars) + chunks
                    > cap
                ):
                    log.info("agent send refused session=%s code=room_chunk_limit", who.session_id)
                    raise ApiError(
                        429, "room_chunk_limit", f"room: at most {cap} chunks per hour to one room"
                    )
                if new_files:
                    size = sum(f.size for f in new_files)
                    if await send_files.room_bytes_last_hour(conn, room_id) + size > (
                        settings.agent_room_file_bytes_per_hour
                    ):
                        raise _room_bytes_limit(who)
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
                await send_files.insert_many(conn, send_id, new_files)
            log.info("agent send queued session=%s id=s%s kind=%s", who.session_id, send_id, kind)
            for f in new_files:
                log.info(
                    "agent send file session=%s id=s%s position=%s size=%s mime=%s sha256=%s",
                    who.session_id,
                    send_id,
                    f.position,
                    f.size,
                    f.mime,
                    f.sha256[:12],
                )
            return SendAccepted(id=f"s{send_id}", status="queued")

    def _room_bytes_limit(who: Caller) -> ApiError:
        log.info("agent send refused session=%s code=room_file_bytes_limit", who.session_id)
        return ApiError(
            429,
            "room_file_bytes_limit",
            f"room: at most {settings.agent_room_file_bytes_per_hour} file bytes per hour to"
            " one room",
        )

    async def _precheck(conn: psycopg.AsyncConnection, who: Caller, room_id: str) -> None:
        """Refuse before any download when the rate or the room byte budget is already spent;
        the locked transaction re-checks both."""
        if (
            await sends.count_recent(conn, who.session_id, RATE_WINDOW_SECONDS)
            >= settings.agent_rate_per_minute
        ):
            log.info("agent send refused session=%s code=rate_limited", who.session_id)
            raise ApiError(
                429,
                "rate_limited",
                f"at most {settings.agent_rate_per_minute} sends per session in 60 s",
            )
        if (
            await send_files.room_bytes_last_hour(conn, room_id)
            >= settings.agent_room_file_bytes_per_hour
        ):
            raise _room_bytes_limit(who)

    async def _fetch_all(
        who: Caller, agent: str | None, paths: list[str]
    ) -> list[send_files.NewFile]:
        try:
            agent_id = uuid.UUID(agent or "")
        except ValueError:
            log.info("agent send refused session=%s code=files_unavailable", who.session_id)
            raise ApiError(
                422,
                "files_unavailable",
                "X-Orch-Agent: the calling agent's uuid is required to send files",
            ) from None
        return [await _fetch(who, agent_id, n, path) for n, path in enumerate(paths)]

    async def _fetch(
        who: Caller, agent_id: uuid.UUID, position: int, path: str
    ) -> send_files.NewFile:
        """One file from the agent's workdir, typed from its bytes, capped, its image metadata
        stripped and hashed. Errors name `files[<position>]`, never the path."""
        field = f"files[{position}]"
        try:
            data = await orchestrator.download_file(
                who.session_id, path, agent_id, max_bytes=settings.agent_file_max_bytes
            )
        except OrchestratorUnavailable as exc:
            log.warning(
                "agent send file download: orchestrator unavailable (%s)", type(exc).__name__
            )
            raise ApiError(503, "unavailable", "orchestrator unavailable") from None
        except OrchestratorError as exc:
            # Status and code only: the message may echo the path.
            log.info(
                "agent send file download refused session=%s position=%s status=%s code=%s",
                who.session_id,
                position,
                exc.status,
                exc.code,
            )
            if exc.status == 404:
                raise ApiError(
                    404, "file_not_found", f"{field}: no such file in the agent's workdir"
                ) from None
            if exc.status == 413:
                raise _too_large(field, settings.agent_file_max_bytes) from None
            raise ApiError(
                422, "files_unavailable", f"{field}: the orchestrator could not provide the file"
            ) from None
        name = media.safe_name(path)
        mime = media.outbound_mime(data, name)
        msgtype = media.outbound_msgtype(mime)
        cap = media.outbound_cap(
            msgtype, settings.agent_image_max_bytes, settings.agent_file_max_bytes
        )
        if len(data) > cap:
            raise _too_large(field, cap)
        try:
            data, w, h = await asyncio.to_thread(media.strip_outbound_metadata, data, mime)
        except media.MediaRefused:
            raise ApiError(
                422, "files_unavailable", f"{field}: the image could not be read"
            ) from None
        if len(data) > cap:
            raise _too_large(field, cap)
        return send_files.NewFile(
            position=position,
            path=path,
            name=name,
            mime=mime,
            msgtype=msgtype,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            w=w,
            h=h,
            data=data,
        )

    @router.get("/rooms", response_model=ChatRooms, responses=_errors(400, 401, 503))
    async def list_rooms(who: Annotated[Caller, Depends(caller)]) -> ChatRooms:
        """The rooms this session may post to: its bound room, else the unbound flagged rooms.
        A room with `anonymize_names` is named with its people's pseudonyms."""
        # The directory is read before the connection is taken: one pool connection at a time.
        directory_rooms = []
        for listed in await directory.list_rooms(pool):
            room = await directory.get_room(pool, listed.room_id)
            if room is not None:  # else forgotten since the listing
                directory_rooms.append(room)
        found = []
        async with pool.connection() as conn:
            rule_list = await rules.list_rules(conn)
            own = await bindings.active_for_session(conn, who.session_id)
            for room in directory_rooms:
                if await may_post(conn, who.session_id, room, rule_list, settings) != "ok":
                    continue
                setting = await rules.get_room_settings(conn, room.room_id)
                name = room.name
                if setting is not None and setting.anonymize_names:
                    name = labeler.room(name, room.kind, room.members, excluded)
                found.append(
                    ChatRoom(
                        room_id=room.room_id,
                        name=name,
                        kind=room.kind,
                        bound=own is not None and own.room_id == room.room_id,
                        agents_may_initiate=setting is not None and setting.agents_may_initiate,
                        agents_may_send_files=setting is not None and setting.agents_may_send_files,
                    )
                )
        found.sort(key=lambda r: (not r.bound, r.name is None, r.name or "", r.room_id))
        return ChatRooms(rooms=found)

    @router.post("/messages", status_code=202, response_model=SendAccepted, responses=send_errors)
    async def send_message(
        who: Annotated[Caller, Depends(caller)],
        body: MessageIn,
        agent: Annotated[
            str | None,
            Header(
                alias="X-Orch-Agent",
                description="the calling agent's uuid, whose workdir holds `files`; required "
                "(else 422 files_unavailable) only when files are sent",
            ),
        ] = None,
    ) -> SendAccepted:
        """Queue a message, optionally a quoted reply, with up to `MATRIX_AGENT_FILES_PER_SEND`
        workdir files sent after the text. In a room with `anonymize_names`, a text or file
        name containing a pseudonym (`user-<sha>`, `admin-<sha>`) is 422
        `pseudonym_disclosed`."""
        text = body.text if body.text is not None and body.text.strip() else None
        if text is None and not body.files:
            raise ApiError(
                422, "invalid_text", "text: needs a non-whitespace character to be delivered"
            )
        if len(body.files) > settings.agent_files_per_send:
            raise ApiError(
                422,
                "invalid_request",
                f"files: at most {settings.agent_files_per_send} files per message",
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
            who,
            kind="message",
            room_id=room_id,
            text=text,
            key=None,
            reply_to=body.reply_to,
            ref_event_id=ref.event_id if ref else None,
            masked_field="reply_to" if ref is not None and ref.source == "inbound" else None,
            files=body.files,
            agent=agent,
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
        files: list[SentFile] = []
        if parsed is not None and parsed[0] == "s":
            async with pool.connection() as conn:
                row = await sends.get(conn, parsed[1])
                if row is not None and row.session_id == who.session_id:
                    delivered = await send_files.delivery(conn, row.id)
                    files = [
                        SentFile(
                            name=f.name,
                            mime=f.mime,
                            size=f.size,
                            status=_file_status(row, f, delivered.get(f.position)),
                        )
                        for f in await send_files.list_for_send(conn, row.id)
                    ]
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
            files=files,
        )

    notes_errors = _errors(400, 401, 409, 413, 422, 503)

    def room_notes(room_id: str, row: notes.NotesRow | None) -> RoomNotes:
        body = row.body if row else ""
        return RoomNotes(
            room_id=room_id,
            body=body,
            version=row.version if row else 0,
            updated_at=row.updated_at if row else None,
            chars=len(body),
            max_chars=settings.room_notes_max_chars,
            rule=memory_rule(settings),
        )

    @router.get("/notes", response_model=RoomNotes, responses=_errors(400, 401, 409, 503))
    async def get_notes(who: Annotated[Caller, Depends(caller)]) -> RoomNotes:
        """The notes of the room this session is bound to (empty, version 0, when none), with
        the room memory rule."""
        async with pool.connection() as conn:
            room_id = await _own_room(conn, who)
            row = await notes.get(conn, room_id)
        log.info(
            "notes read session=%s room=%s version=%s",
            who.session_id,
            room_id,
            row.version if row else 0,
        )
        return room_notes(room_id, row)

    @router.put("/notes", response_model=RoomNotes, responses=notes_errors)
    async def put_notes(who: Annotated[Caller, Depends(caller)], body: NotesIn) -> RoomNotes:
        """Replace the notes of the room this session is bound to, guarded by the version read
        before (0 creates); an empty body forgets them. Over the cap is 413."""
        cap = settings.room_notes_max_chars
        if len(body.body) > cap:
            raise ApiError(
                413,
                "notes_too_large",
                f"body: at most {cap} characters, got {len(body.body)}",
            )
        async with pool.connection() as conn, conn.transaction():
            room_id = await _own_room(conn, who)
            row = await notes.put(conn, room_id, body.body, body.expected_version, who.slug)
            if row is None:
                current = await notes.get(conn, room_id)
                raise _version_conflict(body.expected_version, current.version if current else 0)
        log.info(
            "notes write session=%s room=%s version=%s chars=%s",
            who.session_id,
            room_id,
            row.version,
            len(row.body),
        )
        return room_notes(room_id, row)

    skill_errors = _errors(400, 401, 404, 409, 422, 503)

    def room_skill(row: skills.SkillRow) -> RoomSkill:
        return RoomSkill(
            room_id=row.room_id,
            name=row.name,
            description=row.description,
            body=row.body,
            version=row.version,
            updated_at=row.updated_at,
            chars=len(row.body),
            max_chars=settings.room_skill_max_chars,
        )

    @router.get("/skills", response_model=RoomSkills, responses=_errors(400, 401, 409, 503))
    async def list_skills(who: Annotated[Caller, Depends(caller)]) -> RoomSkills:
        """The skills of the room this session is bound to: name, description and version, no
        bodies."""
        async with pool.connection() as conn:
            room_id = await _own_room(conn, who)
            rows = await skills.list_for_room(conn, room_id)
        log.info("skill list session=%s room=%s count=%s", who.session_id, room_id, len(rows))
        return RoomSkills(
            room_id=room_id,
            skills=[
                SkillMeta(
                    name=row.name,
                    description=row.description,
                    version=row.version,
                    updated_at=row.updated_at,
                    chars=len(row.body),
                )
                for row in rows
            ],
            max_skills=settings.room_skills_max,
            max_chars=settings.room_skill_max_chars,
        )

    @router.get("/skills/{name}", response_model=RoomSkill, responses=skill_errors)
    async def get_skill(who: Annotated[Caller, Depends(caller)], name: str) -> RoomSkill:
        """One skill of the room this session is bound to, with its body."""
        _skill_name(name)
        async with pool.connection() as conn:
            room_id = await _own_room(conn, who)
            row = await skills.get(conn, room_id, name)
        if row is None:
            raise _skill_not_found()
        log.info(
            "skill read session=%s room=%s name=%s version=%s chars=%s",
            who.session_id,
            room_id,
            name,
            row.version,
            len(row.body),
        )
        return room_skill(row)

    @router.put("/skills/{name}", response_model=RoomSkill, responses=_errors(*skill_errors, 413))
    async def put_skill(
        who: Annotated[Caller, Depends(caller)], name: str, body: SkillIn
    ) -> RoomSkill:
        """Create (`expected_version` 0) or replace a skill of the room this session is bound
        to. Over `MATRIX_ROOM_SKILL_MAX_CHARS` is 413; a create past `MATRIX_ROOM_SKILLS_MAX`
        is 409 `skill_limit`."""
        _skill_name(name)
        cap = settings.room_skill_max_chars
        if len(body.body) > cap:
            raise ApiError(
                413, "skill_too_large", f"body: at most {cap} characters, got {len(body.body)}"
            )
        async with pool.connection() as conn, conn.transaction():
            room_id = await _own_room(conn, who)
            await skills.lock_room(conn, room_id)
            existing = await skills.get(conn, room_id, name)
            if existing is None:
                if body.expected_version > 0:
                    raise _skill_not_found()
                if await skills.count(conn, room_id) >= settings.room_skills_max:
                    raise ApiError(
                        409,
                        "skill_limit",
                        f"at most {settings.room_skills_max} skills per room: delete one first",
                    )
            row = await skills.put(
                conn,
                room_id,
                name,
                body.description,
                body.body,
                body.expected_version,
                who.slug,
            )
            if row is None:
                raise _version_conflict(body.expected_version, existing.version if existing else 0)
        log.info(
            "skill write session=%s room=%s name=%s version=%s chars=%s",
            who.session_id,
            room_id,
            name,
            row.version,
            len(row.body),
        )
        return room_skill(row)

    @router.delete("/skills/{name}", response_model=SkillDeleted, responses=skill_errors)
    async def delete_skill(who: Annotated[Caller, Depends(caller)], name: str) -> SkillDeleted:
        """Delete a skill of the room this session is bound to."""
        _skill_name(name)
        async with pool.connection() as conn, conn.transaction():
            room_id = await _own_room(conn, who)
            deleted = await skills.delete(conn, room_id, name)
        if not deleted:
            raise _skill_not_found()
        log.info("skill delete session=%s room=%s name=%s", who.session_id, room_id, name)
        return SkillDeleted(room_id=room_id, name=name, deleted=True)

    return router


def build_admin_router(
    pool: AsyncConnectionPool, orchestrator: Any, settings: ApiSettings
) -> APIRouter:
    """The UI handlers for rules, rooms and users plus the room request routes, narrowed to
    the routes the superleader needs (`_ADMIN_ROUTES`)."""
    admin = APIRouter()
    for router in (
        build_rooms_router(pool, orchestrator, settings),
        build_rules_router(pool, orchestrator),
        build_room_requests_router(pool, settings),
    ):
        admin.routes.extend(
            route
            for route in router.routes
            if any((route.path, m) in _ADMIN_ROUTES for m in getattr(route, "methods", ()))
        )
    return admin


def create_agent_app(
    pool: AsyncConnectionPool, orchestrator: Any, settings: ApiSettings
) -> FastAPI:
    media.quiet_file_urls()
    app = FastAPI(
        title="agents-matrix-connector agent API",
        openapi_url=f"{CHAT_PREFIX}/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(build_chat_router(pool, orchestrator, settings))
    app.include_router(build_admin_router(pool, orchestrator, settings), prefix=ADMIN_PREFIX)
    app.include_router(build_internal_router(pool), prefix=INTERNAL_PREFIX)
    app.add_middleware(AgentGate, token_sha256=settings.agent_token_sha256.get_secret_value())
    add_error_handlers(app)
    add_probes(app, pool)
    return app
