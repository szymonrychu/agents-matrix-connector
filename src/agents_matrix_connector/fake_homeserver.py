"""In-memory Matrix homeserver for tests.

`FakeHomeserver(user_id, token).app` is an ASGI app implementing the client-server calls
`MatrixClient` makes, with Matrix-shaped JSON and errors, for one bot user and its token.
Drive it through `httpx.ASGITransport(app=fake.app)` or serve it with uvicorn; the helpers
(`add_room`, `invite`, `post`, `set_state`, `set_member`, `encrypt`, `fail_next`,
`revoke_token`) change its state and `sent` / `typing_state` read back what the bot did.

Routing works on the raw path, segment by segment, so percent-encoded `/` in a room id or
transaction id stays inside its segment as it does on Synapse.
"""

import asyncio
import copy
import fnmatch
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

ROUTES = (
    "whoami",
    "create_filter",
    "sync",
    "join",
    "room_state",
    "joined_members",
    "send_text",
    "typing",
)
DEFAULT_ERRCODES = {
    400: "M_UNKNOWN",
    401: "M_UNKNOWN_TOKEN",
    403: "M_FORBIDDEN",
    404: "M_NOT_FOUND",
    429: "M_LIMIT_EXCEEDED",
}
PREJOIN_STATE_TYPES = frozenset(
    {
        "m.room.create",
        "m.room.name",
        "m.room.avatar",
        "m.room.topic",
        "m.room.join_rules",
        "m.room.canonical_alias",
        "m.room.encryption",
    }
)
ENCRYPTION_ALGORITHM = "m.megolm.v1.aes-sha2"
BASE_TS = 1_700_000_000_000
PREFIX = ("", "_matrix", "client", "v3")

Handler = Callable[[Request, list[str]], Awaitable[Response]]


def matrix_error(status: int, errcode: str, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"errcode": errcode, "error": error, **extra}, status_code=status)


@dataclass
class _Room:
    room_id: str
    creator: str
    state: dict[tuple[str, str], tuple[int, dict]] = field(default_factory=dict)
    timeline: list[tuple[int, dict]] = field(default_factory=list)


class _AsgiApp:
    """ASGI3 callable object; uvicorn takes a bound coroutine method for an ASGI2 app."""

    def __init__(self, handler: Callable[[Scope, Receive, Send], Awaitable[None]]):
        self._handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._handler(scope, receive, send)


@dataclass(frozen=True)
class _Failure:
    status: int
    errcode: str | None
    retry_after_ms: int | None


class FakeHomeserver:
    def __init__(self, user_id: str, token: str):
        self.user_id = user_id
        self.server_name = user_id.split(":", 1)[1]
        self._token = token
        self._token_valid = True
        self._pos = 0
        self._changed = asyncio.Event()
        self._rooms: dict[str, _Room] = {}
        self._filters: dict[str, dict] = {}
        self._txns: dict[tuple[str, str, str, str], str] = {}
        self._sent: dict[str, list[dict]] = {}
        self._typing: dict[str, bool] = {}
        self._failures: dict[str, list[_Failure]] = {route: [] for route in ROUTES}
        self._table: list[tuple[str, tuple[str | None, ...], str, Handler]] = [
            ("GET", ("account", "whoami"), "whoami", self._whoami),
            ("POST", ("user", None, "filter"), "create_filter", self._create_filter),
            ("GET", ("sync",), "sync", self._sync),
            ("POST", ("join", None), "join", self._join),
            ("GET", ("rooms", None, "state"), "room_state", self._room_state),
            ("GET", ("rooms", None, "joined_members"), "joined_members", self._joined_members),
            ("PUT", ("rooms", None, "send", None, None), "send_text", self._send),
            ("PUT", ("rooms", None, "typing", None), "typing", self._typing_route),
        ]
        self.app = _AsgiApp(self._asgi)

    def __repr__(self) -> str:
        return f"FakeHomeserver(user_id={self.user_id!r})"

    # Helpers driving the fake from a test.

    def add_room(
        self,
        room_id: str,
        state: list[dict],
        members: dict[str, str],
        *,
        join_rule: str = "invite",
    ) -> None:
        """Create a room with the given state events (`type`, `state_key`, `content`,
        optional `sender`) and joined members (user id to display name)."""
        creator = next((u for u in members if u != self.user_id), self.user_id)
        room = _Room(room_id, creator)
        self._rooms[room_id] = room
        self._append(room, "m.room.create", creator, {"creator": creator}, "")
        self._append(room, "m.room.join_rules", creator, {"join_rule": join_rule}, "")
        for user_id, display_name in members.items():
            self._member_event(room, user_id, "join", display_name)
        for event in state:
            self._append(
                room,
                event["type"],
                event.get("sender", creator),
                event.get("content", {}),
                event.get("state_key", ""),
            )

    def invite(self, room_id: str, inviter: str) -> None:
        self._member_event(self._room(room_id), self.user_id, "invite", sender=inviter)

    def post(self, room_id: str, sender: str, content: dict) -> str:
        return self._append(self._room(room_id), "m.room.message", sender, content)["event_id"]

    def set_state(self, room_id: str, event_type: str, state_key: str, content: dict) -> None:
        room = self._room(room_id)
        self._append(room, event_type, room.creator, content, state_key)

    def set_member(
        self,
        room_id: str,
        user_id: str,
        membership: str,
        display_name: str | None = None,
    ) -> None:
        self._member_event(self._room(room_id), user_id, membership, display_name)

    def encrypt(self, room_id: str) -> None:
        self.set_state(room_id, "m.room.encryption", "", {"algorithm": ENCRYPTION_ALGORITHM})

    def sent(self, room_id: str) -> list[dict]:
        """Contents the bot sent into the room, in order, one per distinct transaction."""
        return copy.deepcopy(self._sent.get(room_id, []))

    def typing_state(self, room_id: str) -> bool:
        return self._typing.get(room_id, False)

    def fail_next(
        self,
        route: str,
        status: int,
        errcode: str | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        """Answer the next call of `route` (one of ROUTES) with this error; calls queue."""
        if route not in self._failures:
            raise ValueError(f"unknown route {route!r}, expected one of {ROUTES}")
        self._failures[route].append(_Failure(status, errcode, retry_after_ms))

    def revoke_token(self) -> None:
        self._token_valid = False

    # Internal state.

    def _room(self, room_id: str) -> _Room:
        try:
            return self._rooms[room_id]
        except KeyError:
            raise KeyError(f"unknown room {room_id!r}, add it with add_room first") from None

    def _append(
        self,
        room: _Room,
        event_type: str,
        sender: str,
        content: dict,
        state_key: str | None = None,
    ) -> dict:
        self._pos += 1
        event: dict[str, Any] = {
            "event_id": f"${self._pos}:{self.server_name}",
            "type": event_type,
            "sender": sender,
            "origin_server_ts": BASE_TS + self._pos,
            "content": copy.deepcopy(content),
            "room_id": room.room_id,
            "unsigned": {},
        }
        if state_key is not None:
            event["state_key"] = state_key
            room.state[(event_type, state_key)] = (self._pos, event)
        room.timeline.append((self._pos, event))
        self._changed.set()
        self._changed = asyncio.Event()
        return event

    def _member_event(
        self,
        room: _Room,
        user_id: str,
        membership: str,
        display_name: str | None = None,
        *,
        sender: str | None = None,
    ) -> None:
        content: dict[str, Any] = {"membership": membership}
        if display_name is not None:
            content["displayname"] = display_name
        self._append(room, "m.room.member", sender or user_id, content, user_id)

    def _membership(self, room: _Room) -> tuple[int, str] | None:
        member = room.state.get(("m.room.member", self.user_id))
        if member is None:
            return None
        pos, event = member
        return pos, event["content"]["membership"]

    def _is_joined(self, room: _Room) -> bool:
        membership = self._membership(room)
        return membership is not None and membership[1] == "join"

    # ASGI.

    async def _asgi(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        response = await self._dispatch(request, scope)
        await response(scope, receive, send)

    async def _dispatch(self, request: Request, scope: Scope) -> Response:
        raw = scope.get("raw_path") or scope["path"].encode()
        segments = [unquote(s) for s in raw.decode("latin-1").split("?", 1)[0].split("/")]
        if tuple(segments[: len(PREFIX)]) != PREFIX:
            return matrix_error(404, "M_UNRECOGNIZED", "Unrecognized request")
        rest = segments[len(PREFIX) :]
        method_allowed = False
        for method, pattern, route, handler in self._table:
            if len(pattern) != len(rest) or any(
                p is not None and p != s for p, s in zip(pattern, rest, strict=True)
            ):
                continue
            if method != request.method:
                method_allowed = True
                continue
            denied = self._check_token(request)
            if denied is not None:
                return denied
            if self._failures[route]:
                return _failure_response(self._failures[route].pop(0))
            params = [s for p, s in zip(pattern, rest, strict=True) if p is None]
            return await handler(request, params)
        if method_allowed:
            return matrix_error(405, "M_UNRECOGNIZED", "Unrecognized request")
        return matrix_error(404, "M_UNRECOGNIZED", "Unrecognized request")

    def _check_token(self, request: Request) -> Response | None:
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not token:
            return matrix_error(401, "M_MISSING_TOKEN", "Missing access token")
        if not self._token_valid or token != self._token:
            return matrix_error(
                401, "M_UNKNOWN_TOKEN", "Unrecognised access token", soft_logout=False
            )
        return None

    # Routes.

    async def _whoami(self, request: Request, params: list[str]) -> Response:
        return JSONResponse({"user_id": self.user_id, "device_id": "FAKEDEVICE"})

    async def _create_filter(self, request: Request, params: list[str]) -> Response:
        [user_id] = params
        if user_id != self.user_id:
            return matrix_error(403, "M_FORBIDDEN", "Can only create filters for your own user")
        body = await _json_body(request)
        if not isinstance(body, dict):
            return matrix_error(400, "M_NOT_JSON", "Content not JSON")
        filter_id = str(len(self._filters))
        self._filters[filter_id] = copy.deepcopy(body)
        return JSONResponse({"filter_id": filter_id})

    async def _sync(self, request: Request, params: list[str]) -> Response:
        query = request.query_params
        since: int | None = None
        if "since" in query:
            token = query["since"]
            if not (token.startswith("s") and token[1:].isdigit()):
                return matrix_error(400, "M_INVALID_PARAM", "Invalid since token")
            since = int(token[1:])
        try:
            timeout_ms = int(query.get("timeout", "0"))
        except ValueError:
            return matrix_error(400, "M_INVALID_PARAM", "Invalid timeout")
        full_state = query.get("full_state") == "true"
        definition: dict = {}
        if "filter" in query:
            raw_filter = query["filter"]
            if raw_filter.startswith("{"):
                try:
                    definition = json.loads(raw_filter)
                except ValueError:
                    return matrix_error(400, "M_NOT_JSON", "Filter is not JSON")
            elif raw_filter in self._filters:
                definition = self._filters[raw_filter]
            else:
                return matrix_error(404, "M_NOT_FOUND", "No such filter")
        rooms = self._sync_rooms(since, full_state, definition)
        if since is not None and not full_state and timeout_ms > 0:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_ms / 1000
            while not any(rooms.values()):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except TimeoutError:
                    break
                rooms = self._sync_rooms(since, full_state, definition)
        return JSONResponse({"next_batch": f"s{self._pos}", "rooms": rooms})

    def _sync_rooms(self, since: int | None, full_state: bool, definition: dict) -> dict[str, dict]:
        room_filter = definition.get("room", {})
        timeline_types = room_filter.get("timeline", {}).get("types")
        state_types = room_filter.get("state", {}).get("types")
        invite: dict[str, dict] = {}
        join: dict[str, dict] = {}
        leave: dict[str, dict] = {}
        for room in self._rooms.values():
            membership = self._membership(room)
            if membership is None:
                continue
            member_pos, state = membership
            changed = since is None or member_pos > since
            if state == "invite" and changed:
                invite[room.room_id] = {"invite_state": {"events": self._stripped_state(room)}}
            elif state == "join":
                timeline = [
                    _client_event(e)
                    for pos, e in room.timeline
                    if (since is None or pos > since) and _wanted(e, timeline_types)
                ]
                state_events = []
                if full_state or changed:
                    state_events = [
                        _client_event(e)
                        for _, e in sorted(room.state.values(), key=lambda item: item[0])
                        if _wanted(e, state_types)
                    ]
                if timeline or state_events:
                    join[room.room_id] = _room_body(state_events, timeline)
            elif state in ("leave", "ban") and since is not None and changed:
                timeline = [
                    _client_event(e)
                    for pos, e in room.timeline
                    if since < pos <= member_pos and _wanted(e, timeline_types)
                ]
                leave[room.room_id] = _room_body([], timeline)
        return {"invite": invite, "join": join, "leave": leave}

    def _stripped_state(self, room: _Room) -> list[dict]:
        events = [
            e
            for _, e in sorted(room.state.values(), key=lambda item: item[0])
            if e["type"] in PREJOIN_STATE_TYPES
            or (e["type"] == "m.room.member" and e["state_key"] == self.user_id)
        ]
        return [
            {k: copy.deepcopy(e[k]) for k in ("type", "state_key", "sender", "content")}
            for e in events
        ]

    async def _join(self, request: Request, params: list[str]) -> Response:
        [room_id] = params
        room = self._rooms.get(room_id)
        if room is None:
            return matrix_error(404, "M_NOT_FOUND", "No known servers")
        membership = self._membership(room)
        current = membership[1] if membership else None
        if current == "join":
            return JSONResponse({"room_id": room_id})
        if current == "ban":
            return matrix_error(403, "M_FORBIDDEN", "You are banned from this room")
        join_rule = room.state[("m.room.join_rules", "")][1]["content"]["join_rule"]
        if current != "invite" and join_rule != "public":
            return matrix_error(403, "M_FORBIDDEN", "You are not invited to this room.")
        self._member_event(room, self.user_id, "join")
        return JSONResponse({"room_id": room_id})

    def _joined_room(self, room_id: str) -> _Room | Response:
        room = self._rooms.get(room_id)
        if room is None:
            return matrix_error(404, "M_NOT_FOUND", "Unknown room")
        if not self._is_joined(room):
            return matrix_error(403, "M_FORBIDDEN", f"User {self.user_id} not in room {room_id}")
        return room

    async def _room_state(self, request: Request, params: list[str]) -> Response:
        room = self._joined_room(params[0])
        if isinstance(room, Response):
            return room
        events = [e for _, e in sorted(room.state.values(), key=lambda item: item[0])]
        return JSONResponse(copy.deepcopy(events))

    async def _joined_members(self, request: Request, params: list[str]) -> Response:
        room = self._joined_room(params[0])
        if isinstance(room, Response):
            return room
        joined = {
            state_key: {
                "display_name": event["content"].get("displayname"),
                "avatar_url": event["content"].get("avatar_url"),
            }
            for (event_type, state_key), (_, event) in room.state.items()
            if event_type == "m.room.member" and event["content"]["membership"] == "join"
        }
        return JSONResponse({"joined": joined})

    async def _send(self, request: Request, params: list[str]) -> Response:
        room_id, event_type, txn_id = params
        room = self._joined_room(room_id)
        if isinstance(room, Response):
            return room
        key = (self._token, room_id, event_type, txn_id)
        if key in self._txns:
            return JSONResponse({"event_id": self._txns[key]})
        content = await _json_body(request)
        if not isinstance(content, dict):
            return matrix_error(400, "M_NOT_JSON", "Content not JSON")
        event = self._append(room, event_type, self.user_id, content)
        self._txns[key] = event["event_id"]
        self._sent.setdefault(room_id, []).append(copy.deepcopy(content))
        return JSONResponse({"event_id": event["event_id"]})

    async def _typing_route(self, request: Request, params: list[str]) -> Response:
        room_id, user_id = params
        if user_id != self.user_id:
            return matrix_error(403, "M_FORBIDDEN", "Cannot set another user's typing state")
        room = self._joined_room(room_id)
        if isinstance(room, Response):
            return room
        body = await _json_body(request)
        if not isinstance(body, dict) or not isinstance(body.get("typing"), bool):
            return matrix_error(400, "M_BAD_JSON", "Missing typing key")
        self._typing[room_id] = body["typing"]
        return JSONResponse({})


def _wanted(event: dict, types: list[str] | None) -> bool:
    return types is None or any(fnmatch.fnmatchcase(event["type"], t) for t in types)


def _client_event(event: dict) -> dict:
    """A sync event: the stored event without `room_id` (ClientEventWithoutRoomID)."""
    return {k: copy.deepcopy(v) for k, v in event.items() if k != "room_id"}


def _room_body(state: list[dict], timeline: list[dict]) -> dict:
    return {
        "state": {"events": state},
        "timeline": {"events": timeline, "limited": False},
    }


def _failure_response(failure: _Failure) -> JSONResponse:
    errcode = failure.errcode or DEFAULT_ERRCODES.get(failure.status, "M_UNKNOWN")
    extra = {} if failure.retry_after_ms is None else {"retry_after_ms": failure.retry_after_ms}
    return matrix_error(failure.status, errcode, f"Injected failure {failure.status}", **extra)


async def _json_body(request: Request) -> Any:
    try:
        return json.loads(await request.body() or b"{}")
    except ValueError:
        return None
