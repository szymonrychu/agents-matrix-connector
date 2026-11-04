"""The bot step that resolves and executes room requests, against the fake homeserver and a
fake provisioning API."""

import json

import httpx
import pytest

from agents_matrix_connector.bridge import ProvisioningClient
from agents_matrix_connector.db import room_requests as rr
from agents_matrix_connector.fake_homeserver import FakeHomeserver
from agents_matrix_connector.matrix import MatrixAuthError, MatrixClient
from agents_matrix_connector.room_requests import ACCEPT_EVENT, RoomRequests
from agents_matrix_connector.settings import BotSettings

BOT = "@agents:x"
TOKEN = "hs-secret-token"
GHOST = "@facebook_100:x"


class FakeBridge:
    """search_users by query, create_dm by id; `down` answers 502 to everything."""

    def __init__(self):
        self.search: dict[str, list[dict]] = {}
        self.dms: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.down:
            return httpx.Response(502, json={"errcode": "M_UNKNOWN", "error": "down"})
        path = request.url.path.removeprefix("/_matrix/provision/v3")
        if path == "/search_users":
            query = json.loads(request.content)["query"]
            return httpx.Response(200, json={"results": self.search.get(query, [])})
        if path.startswith("/create_dm/"):
            fb = path.rsplit("/", 1)[1]
            if fb not in self.dms:
                return httpx.Response(404, json={"errcode": "M_NOT_FOUND", "error": "unknown"})
            return httpx.Response(201, json=self.dms[fb])
        return httpx.Response(404, json={"errcode": "M_UNRECOGNIZED", "error": "no route"})


def settings(url) -> BotSettings:
    return BotSettings(
        database_url=url,
        orchestrator_url="http://orch",
        orchestrator_token="t",
        homeserver_url="http://hs",
        user_id=BOT,
        access_token=TOKEN,
    )


@pytest.fixture
def hs():
    return FakeHomeserver(BOT, TOKEN)


@pytest.fixture
def fake_bridge():
    return FakeBridge()


@pytest.fixture
async def matrix(hs):
    async with MatrixClient("http://hs", TOKEN, transport=httpx.ASGITransport(app=hs.app)) as c:
        yield c


@pytest.fixture
async def bridge(fake_bridge):
    c = ProvisioningClient(
        "http://bridge/_matrix/provision",
        TOKEN,
        BOT,
        transport=httpx.MockTransport(fake_bridge.handler),
    )
    yield c
    await c.aclose()


@pytest.fixture
def step(pool, matrix, bridge, url):
    return RoomRequests(pool, matrix, bridge, settings(url))


async def put(conn, **kw):
    kw.setdefault("action", "open")
    kw.setdefault("status", "confirmed")
    kw.setdefault("task_key", "T-25")
    return await rr.insert(conn, **kw)


async def get(conn, row):
    return await rr.get(conn, row.id)


async def test_resolving_unique_match_pending_confirmation(step, conn, fake_bridge):
    fake_bridge.search["jane doe"] = [
        {"id": "100", "name": "Jane Doe", "mxid": GHOST},
        {"id": "101", "name": "Jane Doer"},
    ]
    row = await put(conn, status="resolving", target_kind="name", target="jane doe")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.target_kind, got.target, got.display_name) == (
        "pending_confirmation",
        "facebook",
        "100",
        "Jane Doe",
    )
    assert got.decision_id is None


async def test_resolving_single_fuzzy_result(step, conn, fake_bridge):
    fake_bridge.search["Jan"] = [{"id": "100", "name": "Jane Doe"}]
    row = await put(conn, status="resolving", target_kind="name", target="Jan")
    await step.tick()
    assert (await get(conn, row)).status == "pending_confirmation"


async def test_resolving_many_candidates(step, conn, fake_bridge):
    fake_bridge.search["Jane"] = [
        {"id": "100", "name": "Jane A", "mxid": GHOST},
        {"id": "101", "name": "Jane B"},
        {"id": "102", "name": "Jane C"},
    ]
    row = await put(conn, status="resolving", target_kind="name", target="Jane")
    await step.tick()
    got = await get(conn, row)
    assert got.status == "candidates"
    assert got.candidates == [
        {"user_id": GHOST, "display_name": "Jane A", "facebook_id": "100"},
        {"user_id": None, "display_name": "Jane B", "facebook_id": "101"},
        {"user_id": None, "display_name": "Jane C", "facebook_id": "102"},
    ]


async def test_resolving_none_failed(step, conn):
    row = await put(conn, status="resolving", target_kind="name", target="Nobody")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.reason) == ("failed", "no_match")


async def test_resolving_bridge_down_stays(step, conn, fake_bridge):
    fake_bridge.down = True
    row = await put(conn, status="resolving", target_kind="name", target="Jane")
    await step.tick()
    assert (await get(conn, row)).status == "resolving"


async def test_resolving_without_bridge_fails(pool, matrix, conn, url):
    step = RoomRequests(pool, matrix, None, settings(url))
    row = await put(conn, status="resolving", target_kind="name", target="Jane")
    fb = await put(conn, target_kind="facebook", target="100")
    await step.tick()
    assert ((await get(conn, row)).status, (await get(conn, row)).reason) == (
        "failed",
        "bridge_unconfigured",
    )
    assert ((await get(conn, fb)).status, (await get(conn, fb)).reason) == (
        "failed",
        "bridge_unconfigured",
    )


async def test_open_facebook_creates_dm_and_joins(step, conn, hs, fake_bridge):
    hs.add_room("!portal:x", [], {GHOST: "Jane"})
    hs.invite("!portal:x", "@facebookbot:x")
    fake_bridge.dms["100"] = {
        "id": "100",
        "name": "Jane",
        "mxid": GHOST,
        "dm_room_mxid": "!portal:x",
    }
    row = await put(conn, target_kind="facebook", target="100")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.room_id, got.reason) == ("done", "!portal:x", None)
    assert got.done_at is not None
    assert hs.membership_of("!portal:x", BOT) == "join"
    assert ("POST", "/_matrix/provision/v3/create_dm/100") in fake_bridge.calls


async def test_open_facebook_join_pending(step, conn, hs, fake_bridge):
    fake_bridge.dms["100"] = {"id": "100", "dm_room_mxid": "!notyet:x"}
    row = await put(conn, target_kind="facebook", target="100")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.room_id, got.reason) == ("done", "!notyet:x", "join_pending")
    assert await rr.joinable_room_ids(conn) == {"!notyet:x"}


async def test_open_facebook_bridge_down_retries(step, conn, hs, fake_bridge):
    fake_bridge.down = True
    row = await put(conn, target_kind="facebook", target="100")
    await step.tick()
    assert (await get(conn, row)).status == "confirmed"
    fake_bridge.down = False
    hs.add_room("!portal:x", [], {GHOST: "Jane"})
    hs.invite("!portal:x", "@facebookbot:x")
    fake_bridge.dms["100"] = {"id": "100", "dm_room_mxid": "!portal:x"}
    await step.tick()
    assert (await get(conn, row)).status == "done"


async def test_open_facebook_unknown_fails(step, conn):
    row = await put(conn, target_kind="facebook", target="404")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.reason) == ("failed", "bridge_error: M_NOT_FOUND")


async def test_open_matrix_creates_room_and_m_direct(step, conn, hs, matrix):
    await matrix.set_account_data(BOT, "m.direct", {"@carol:y": ["!old:x"]})
    row = await put(conn, target_kind="matrix", target="@carol:y")
    await step.tick()
    got = await get(conn, row)
    assert got.status == "done"
    [created] = hs.created_rooms()
    assert created["room_id"] == got.room_id
    assert created["invite"] == ["@carol:y"] and created["is_direct"] is True
    assert hs.account_data("m.direct") == {"@carol:y": ["!old:x", got.room_id]}


async def test_open_matrix_without_m_direct(step, conn, hs):
    row = await put(conn, target_kind="matrix", target="@carol:y")
    await step.tick()
    got = await get(conn, row)
    assert hs.account_data("m.direct") == {"@carol:y": [got.room_id]}


async def test_invite_matrix_user(step, conn, hs):
    hs.add_room("!mx:x", [], {BOT: "Bot", "@a:y": "A"})
    row = await put(conn, action="invite", target_kind="matrix", target="@carol:y", room_id="!mx:x")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.room_id) == ("done", "!mx:x")
    assert hs.invites() == [("!mx:x", "@carol:y")]


async def test_invite_forbidden_fails(step, conn, hs):
    row = await put(
        conn, action="invite", target_kind="matrix", target="@carol:y", room_id="!gone:x"
    )
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.reason) == ("failed", "matrix_error: M_NOT_FOUND")


async def add_db_room(conn, room_id, membership, message_request=True):
    await conn.execute(
        "INSERT INTO rooms (room_id, membership, message_request) VALUES (%s, %s, %s)",
        (room_id, membership, message_request),
    )


async def test_accept_waits_for_join(step, conn, hs):
    await add_db_room(conn, "!req:x", "invite")
    row = await put(conn, action="accept", room_id="!req:x")
    await step.tick()
    assert (await get(conn, row)).status == "confirmed"
    assert hs.created_rooms() == []


async def test_accept_sends_event(step, conn, hs):
    hs.add_room("!req:x", [], {BOT: "Bot", GHOST: "Jane"})
    await add_db_room(conn, "!req:x", "join")
    row = await put(conn, action="accept", room_id="!req:x")
    await step.tick()
    assert (await get(conn, row)).status == "done"
    assert hs.sent_events("!req:x") == [(ACCEPT_EVENT, {})]
    assert ACCEPT_EVENT == "com.beeper.accept_message_request"


async def test_accept_not_message_request_done(step, conn, hs):
    await add_db_room(conn, "!plain:x", "join", message_request=False)
    row = await put(conn, action="accept", room_id="!plain:x")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.reason) == ("done", "not_a_message_request")


async def test_accept_room_left_fails(step, conn):
    await add_db_room(conn, "!left:x", "leave")
    row = await put(conn, action="accept", room_id="!left:x")
    await step.tick()
    got = await get(conn, row)
    assert (got.status, got.reason) == ("failed", "room_left")


async def test_pending_confirmation_never_executed(step, conn, hs, fake_bridge):
    fake_bridge.dms["100"] = {"id": "100", "dm_room_mxid": "!portal:x"}
    row = await put(conn, status="pending_confirmation", target_kind="facebook", target="100")
    await rr.attach_decision(conn, row.id, "T-25", 9)
    candidates = await put(conn, status="candidates", target_kind="name", target="J")
    rejected = await put(conn, status="pending_confirmation", target_kind="facebook", target="100")
    await rr.attach_decision(conn, rejected.id, "T-25", 10)
    await rr.confirm(conn, rejected.id, 10, False)
    await step.tick()
    assert (await get(conn, row)).status == "pending_confirmation"
    assert (await get(conn, candidates)).status == "candidates"
    assert (await get(conn, rejected)).status == "rejected"
    assert fake_bridge.calls == []
    assert hs.created_rooms() == []


async def test_auth_error_propagates(step, conn, hs):
    hs.revoke_token()
    row = await put(conn, target_kind="matrix", target="@carol:y")
    with pytest.raises(MatrixAuthError):
        await step.tick()
    assert (await get(conn, row)).status == "failed"  # the room may exist: never run twice


async def test_restart_fails_working_matrix_open_as_interrupted(
    pool, matrix, bridge, url, conn, hs
):
    row = await put(conn, target_kind="matrix", target="@carol:y", status="working")
    fb = await put(conn, target_kind="facebook", target="100", status="working")
    inv = await put(conn, action="invite", target="@carol:y", room_id="!r:x", status="working")
    await RoomRequests(pool, matrix, bridge, settings(url))._recover()
    got = await get(conn, row)
    assert (got.status, got.reason) == ("failed", "interrupted")
    assert (await get(conn, fb)).status == "confirmed"
    assert (await get(conn, inv)).status == "confirmed"
    assert hs.created_rooms() == []


async def test_unexpected_error_fails_matrix_open_as_interrupted(step, conn, hs, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(step, "_run", boom)
    row = await put(conn, target_kind="matrix", target="@carol:y")
    fb = await put(conn, target_kind="facebook", target="100")
    with pytest.raises(RuntimeError):
        await step._execute()
    got = await get(conn, row)
    assert (got.status, got.reason) == ("failed", "interrupted")
    assert (await get(conn, fb)).status in ("confirmed", "working")
