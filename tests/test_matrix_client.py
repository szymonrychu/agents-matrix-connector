import asyncio
import json
import logging
import traceback

import httpx
import pytest
import uvicorn

from agents_matrix_connector import matrix
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import (
    MatrixAuthError,
    MatrixClient,
    MatrixError,
    MatrixForbidden,
    MatrixNotFound,
    MatrixUnavailable,
)

BOT = "@bot:hs"
TOKEN = "syt_c2VjcmV0_very_secret_token"
ROOM = "!a:hs"
ALICE = "@facebook_1:hs"
BASE = "http://hs"
API = "/_matrix/client/v3"


class Recorder(httpx.AsyncBaseTransport):
    """Forwards to an inner transport and keeps every request it saw."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self.inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self.inner.handle_async_request(request)

    def calls(self) -> list[tuple[str, str]]:
        return [(r.method, r.url.raw_path.decode().split("?")[0]) for r in self.requests]


@pytest.fixture
def fake() -> FakeHomeserver:
    return FakeHomeserver(BOT, TOKEN)


@pytest.fixture
def recorder(fake) -> Recorder:
    return Recorder(httpx.ASGITransport(app=fake.app))


@pytest.fixture
async def client(recorder):
    async with MatrixClient(BASE, TOKEN, transport=recorder) as c:
        yield c


def name_event(name: str) -> dict:
    return {"type": "m.room.name", "state_key": "", "content": {"name": name}}


def bridge_event() -> dict:
    return {
        "type": "m.bridge",
        "state_key": "fb",
        "content": {"com.beeper.room_type.v2": "dm", "channel": {"displayname": "Alice"}},
    }


def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


async def test_calls_and_paths(fake, recorder, client):
    fake.add_room(ROOM, [name_event("Chat"), bridge_event()], {ALICE: "Alice Smith"})
    fake.invite(ROOM, ALICE)

    assert await client.whoami() == BOT
    filter_id = await client.create_filter(
        BOT, {"room": {"timeline": {"types": ["m.room.message"]}}}
    )
    assert isinstance(filter_id, str) and filter_id
    assert await client.join(ROOM) == ROOM
    state = await client.room_state(ROOM)
    assert {(e["type"], e["state_key"]) for e in state} >= {
        ("m.room.name", ""),
        ("m.bridge", "fb"),
        ("m.room.join_rules", ""),
        ("m.room.member", ALICE),
        ("m.room.member", BOT),
    }
    assert next(e for e in state if e["type"] == "m.room.name")["content"] == {"name": "Chat"}
    assert await client.joined_members(ROOM) == {ALICE: "Alice Smith", BOT: None}
    event_id = await client.send_text(ROOM, "txn/1", "hello")
    assert isinstance(event_id, str) and event_id.startswith("$")
    assert fake.sent(ROOM) == [{"msgtype": "m.text", "body": "hello"}]
    await client.typing(BOT, ROOM, True)
    assert fake.typing_state(ROOM) is True
    await client.typing(BOT, ROOM, False)
    assert fake.typing_state(ROOM) is False

    room = "%21a%3Ahs"
    bot = "%40bot%3Ahs"
    assert recorder.calls() == [
        ("GET", f"{API}/account/whoami"),
        ("POST", f"{API}/user/{bot}/filter"),
        ("POST", f"{API}/join/{room}"),
        ("GET", f"{API}/rooms/{room}/state"),
        ("GET", f"{API}/rooms/{room}/joined_members"),
        ("PUT", f"{API}/rooms/{room}/send/m.room.message/txn%2F1"),
        ("PUT", f"{API}/rooms/{room}/typing/{bot}"),
        ("PUT", f"{API}/rooms/{room}/typing/{bot}"),
    ]
    for request in recorder.requests:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
    send, typing_on, typing_off = recorder.requests[5:]
    assert json.loads(send.content) == {"msgtype": "m.text", "body": "hello"}
    assert json.loads(typing_on.content) == {"typing": True, "timeout": 30000}
    assert json.loads(typing_off.content) == {"typing": False}
    assert json.loads(recorder.requests[1].content) == {
        "room": {"timeline": {"types": ["m.room.message"]}}
    }


async def test_sync_shape_and_long_poll(fake, recorder, client):
    fake.add_room(ROOM, [name_event("Chat")], {ALICE: "Alice Smith"})
    fake.invite(ROOM, ALICE)

    first = await client.sync(None, 30000, None)
    assert set(first) >= {"next_batch", "rooms"}
    assert set(first["rooms"]) == {"invite", "join", "leave"}
    assert first["rooms"]["join"] == {} and first["rooms"]["leave"] == {}
    invite_state = first["rooms"]["invite"][ROOM]["invite_state"]["events"]
    assert "m.room.name" in types(invite_state)
    member = next(e for e in invite_state if e["type"] == "m.room.member")
    assert member["state_key"] == BOT and member["sender"] == ALICE
    assert member["content"]["membership"] == "invite"
    request = recorder.requests[-1]
    assert request.url.params.get("timeout") == "30000"
    assert "since" not in request.url.params and "filter" not in request.url.params
    assert request.extensions["timeout"]["read"] == 40.0

    await client.join(ROOM)
    filter_id = await client.create_filter(
        BOT, {"room": {"timeline": {"types": ["m.room.message", "m.room.member"]}}}
    )
    joined = await client.sync(first["next_batch"], 0, filter_id)
    request = recorder.requests[-1]
    assert request.url.params["since"] == first["next_batch"]
    assert request.url.params["filter"] == filter_id
    assert request.extensions["timeout"]["read"] == 10.0
    assert joined["next_batch"] != first["next_batch"]
    assert joined["rooms"]["invite"] == {}
    room = joined["rooms"]["join"][ROOM]
    assert ("m.room.name", "") in {(e["type"], e["state_key"]) for e in room["state"]["events"]}
    assert room["timeline"]["limited"] is False
    assert [(e["type"], e["state_key"]) for e in room["timeline"]["events"]] == [
        ("m.room.member", BOT)
    ]

    message_id = fake.post(ROOM, ALICE, {"msgtype": "m.text", "body": "hi"})
    fake.set_state(ROOM, "m.room.topic", "", {"topic": "filtered out"})
    later = await client.sync(joined["next_batch"], 0, filter_id)
    room = later["rooms"]["join"][ROOM]
    assert room["state"]["events"] == []
    [event] = room["timeline"]["events"]
    assert event["event_id"] == message_id
    assert event["sender"] == ALICE
    assert event["content"] == {"msgtype": "m.text", "body": "hi"}
    assert isinstance(event["origin_server_ts"], int)

    full = await client.sync(later["next_batch"], 0, filter_id, full_state=True)
    assert recorder.requests[-1].url.params["full_state"] == "true"
    full_state = full["rooms"]["join"][ROOM]["state"]["events"]
    assert ("m.room.member", ALICE) in {(e["type"], e["state_key"]) for e in full_state}

    poll = asyncio.create_task(client.sync(later["next_batch"], 5000, filter_id))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not poll.done()
    woke_id = fake.post(ROOM, ALICE, {"msgtype": "m.text", "body": "wake"})
    woke = await asyncio.wait_for(poll, 2)
    assert [e["event_id"] for e in woke["rooms"]["join"][ROOM]["timeline"]["events"]] == [woke_id]

    empty = await client.sync(woke["next_batch"], 20, filter_id)
    assert empty["rooms"] == {"invite": {}, "join": {}, "leave": {}}
    assert empty["next_batch"] == woke["next_batch"]

    fake.set_member(ROOM, BOT, "leave")
    left = await client.sync(woke["next_batch"], 0, filter_id)
    assert ROOM not in left["rooms"]["join"]
    leave_timeline = left["rooms"]["leave"][ROOM]["timeline"]["events"]
    assert leave_timeline[-1]["state_key"] == BOT
    assert leave_timeline[-1]["content"]["membership"] == "leave"


async def test_error_mapping(fake, client):
    cases = [
        (401, None, MatrixAuthError, "M_UNKNOWN_TOKEN"),
        (403, "M_UNKNOWN_TOKEN", MatrixAuthError, "M_UNKNOWN_TOKEN"),
        (403, "M_MISSING_TOKEN", MatrixAuthError, "M_MISSING_TOKEN"),
        (403, None, MatrixForbidden, "M_FORBIDDEN"),
        (404, None, MatrixNotFound, "M_NOT_FOUND"),
        (400, "M_BAD_JSON", MatrixError, "M_BAD_JSON"),
        (500, None, MatrixUnavailable, "M_UNKNOWN"),
        (502, None, MatrixUnavailable, "M_UNKNOWN"),
    ]
    for status, errcode, cls, expected_errcode in cases:
        fake.fail_next("whoami", status, errcode)
        with pytest.raises(MatrixError) as exc:
            await client.whoami()
        assert type(exc.value) is cls, (status, errcode)
        assert exc.value.status == status
        assert exc.value.errcode == expected_errcode
        assert exc.value.error
    for cls in (MatrixAuthError, MatrixForbidden, MatrixNotFound, MatrixUnavailable):
        assert issubclass(cls, MatrixError)

    with pytest.raises(MatrixNotFound) as missing:
        await client.room_state("!nowhere:hs")
    assert missing.value.errcode == "M_NOT_FOUND"

    fake.revoke_token()
    with pytest.raises(MatrixAuthError) as revoked:
        await client.whoami()
    assert (revoked.value.status, revoked.value.errcode) == (401, "M_UNKNOWN_TOKEN")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with MatrixClient(BASE, TOKEN, transport=httpx.MockTransport(refuse)) as down:
        with pytest.raises(MatrixUnavailable) as transport:
            await down.whoami()
    assert transport.value.status is None
    assert transport.value.errcode is None
    assert "connection refused" in transport.value.error


async def test_rate_limit_retry_once(fake, monkeypatch, client):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(matrix, "_sleep", fake_sleep)
    fake.add_room(ROOM, [], {BOT: "Bot", ALICE: "Alice"})

    fake.fail_next("send_text", 429, retry_after_ms=1500)
    event_id = await client.send_text(ROOM, "t1", "hi")
    assert event_id
    assert slept == [1.5]
    assert fake.sent(ROOM) == [{"msgtype": "m.text", "body": "hi"}]

    slept.clear()
    fake.fail_next("send_text", 429, retry_after_ms=60000)
    fake.fail_next("send_text", 429, retry_after_ms=60000)
    with pytest.raises(MatrixUnavailable) as exc:
        await client.send_text(ROOM, "t2", "again")
    assert slept == [10.0]
    assert (exc.value.status, exc.value.errcode) == (429, "M_LIMIT_EXCEEDED")
    assert fake.sent(ROOM) == [{"msgtype": "m.text", "body": "hi"}]


async def test_txn_id_dedupe(fake, client):
    fake.add_room(ROOM, [], {BOT: "Bot", ALICE: "Alice"})
    first = await client.send_text(ROOM, "mxc-1-0", "hello")
    again = await client.send_text(ROOM, "mxc-1-0", "hello")
    assert again == first
    assert fake.sent(ROOM) == [{"msgtype": "m.text", "body": "hello"}]
    other = await client.send_text(ROOM, "mxc-1-1", "second")
    assert other != first
    assert fake.sent(ROOM) == [
        {"msgtype": "m.text", "body": "hello"},
        {"msgtype": "m.text", "body": "second"},
    ]
    sync = await client.sync(None, 0, None)
    bodies = [
        e["content"]["body"]
        for e in sync["rooms"]["join"][ROOM]["timeline"]["events"]
        if e["type"] == "m.room.message"
    ]
    assert bodies == ["hello", "second"]


async def test_join_needs_invite(fake, client):
    fake.add_room(ROOM, [], {ALICE: "Alice"})
    with pytest.raises(MatrixForbidden) as exc:
        await client.join(ROOM)
    assert (exc.value.status, exc.value.errcode) == (403, "M_FORBIDDEN")
    with pytest.raises(MatrixForbidden):
        await client.send_text(ROOM, "t1", "not a member")
    assert fake.sent(ROOM) == []

    fake.invite(ROOM, ALICE)
    assert await client.join(ROOM) == ROOM
    assert BOT in await client.joined_members(ROOM)

    fake.add_room("!public:hs", [], {ALICE: "Alice"}, join_rule="public")
    assert await client.join("!public:hs") == "!public:hs"
    with pytest.raises(MatrixNotFound):
        await client.join("!nowhere:hs")


async def test_token_redacted(fake, client, monkeypatch, caplog):
    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(matrix, "_sleep", no_sleep)
    caplog.set_level(logging.DEBUG)
    assert TOKEN not in repr(client)
    assert TOKEN not in str(client)

    errors: list[MatrixError] = []
    fake.fail_next("whoami", 429, retry_after_ms=1)
    fake.fail_next("whoami", 429, retry_after_ms=1)
    with pytest.raises(MatrixUnavailable) as limited:
        await client.whoami()
    errors.append(limited.value)
    fake.revoke_token()
    with pytest.raises(MatrixAuthError) as revoked:
        await client.whoami()
    errors.append(revoked.value)

    def echo_token(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errcode": "M_UNKNOWN", "error": f"bad {TOKEN}"})

    def leak_in_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused with {TOKEN}", request=request)

    for handler in (echo_token, leak_in_transport):
        async with MatrixClient(BASE, TOKEN, transport=httpx.MockTransport(handler)) as other:
            assert TOKEN not in repr(other)
            with pytest.raises(MatrixUnavailable) as exc:
                await other.whoami()
            errors.append(exc.value)

    for error in errors:
        assert TOKEN not in str(error)
        assert TOKEN not in repr(error)
        assert TOKEN not in error.error
        assert TOKEN not in "".join(traceback.format_exception(error))
    assert TOKEN not in caplog.text


def test_fake_app_is_asgi3_for_uvicorn(fake):
    config = uvicorn.Config(fake.app, lifespan="on")
    config.load()
    assert config.interface == "asgi3"
