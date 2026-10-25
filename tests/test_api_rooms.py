import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import psycopg
import pytest
from agents_common.db.pool import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient

from agents_matrix_connector.api.app import create_api_app
from agents_matrix_connector.settings import ConnectorSettings

PACKAGE = "agents_matrix_connector"
PREFIX = "/matrix/api/v1"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

DM = "!dm:x"
GRP = "!grp:x"
ENC = "!enc:x"
INFRA = "!infra:x"
ALICE = "@facebook_111:x"
BOB = "@facebook_222:x"
BOT = "@agents:x"


def make_settings(**extra) -> ConnectorSettings:
    return ConnectorSettings(
        database_url="postgresql://u:p@db/x",
        orchestrator_url="http://orch",
        orchestrator_token="tok",
        homeserver_url="http://hs:8008",
        user_id=BOT,
        sync_timeout_seconds=30,
        **extra,
    )


@pytest.fixture
def url(migrated_db):
    return migrated_db(PACKAGE)


@pytest.fixture
async def conn(url):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
        yield c


@pytest.fixture
async def pool(url):
    p = await open_pool(url)
    yield p
    await p.close()


@pytest.fixture
def fake():
    return FakeOrchestrator()


@pytest.fixture
async def orchestrator(fake):
    transport = httpx.ASGITransport(app=fake.app)
    async with OrchestratorClient("http://orch", fake.token, transport=transport) as c:
        yield c


@pytest.fixture
async def api(pool, orchestrator):
    app = create_api_app(pool, orchestrator, make_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api" + PREFIX
    ) as c:
        yield c


async def seed(conn):
    for room_id, kind, name, alias, encrypted, members, last in [
        (DM, "dm", "Alice", None, False, 2, T0 + timedelta(minutes=3)),
        (GRP, "group", "Team", "#team:x", False, 3, T0 + timedelta(minutes=2)),
        (ENC, "group", "Secret", None, True, 3, T0 + timedelta(minutes=1)),
        (INFRA, "infrastructure", "Space", None, False, 1, None),
    ]:
        await conn.execute(
            "INSERT INTO rooms (room_id, kind, name, alias, encrypted, membership, member_count,"
            " last_event_at) VALUES (%s, %s, %s, %s, %s, 'join', %s, %s)",
            (room_id, kind, name, alias, encrypted, members, last),
        )
    for user_id, name, fb in [
        (ALICE, "Alice A", "111"),
        (BOB, "Bob B", "222"),
        (BOT, "Agents", None),
    ]:
        await conn.execute(
            "INSERT INTO users (user_id, display_name, facebook_id) VALUES (%s, %s, %s)",
            (user_id, name, fb),
        )
    for room_id, user_id in [(DM, ALICE), (DM, BOB), (GRP, ALICE), (GRP, BOT)]:
        await conn.execute(
            "INSERT INTO room_members (room_id, user_id, membership) VALUES (%s, %s, 'join')",
            (room_id, user_id),
        )
    await conn.execute(
        "INSERT INTO access_rules (list, kind, value) VALUES ('deny', 'user', %s)", (BOB,)
    )
    await conn.execute(
        "INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'room', %s)", (DM,)
    )


async def add_inbound(conn, n, room=DM, status="handled", run_id=None, at=None):
    await conn.execute(
        "INSERT INTO inbound (event_id, room_id, sender, sender_name, body, status, run_id,"
        " received_at) VALUES (%s, %s, %s, 'Alice A', %s, %s, %s, %s)",
        (
            f"$e{n}{room}",
            room,
            ALICE,
            f"hello {n}",
            status,
            run_id,
            at or T0 + timedelta(seconds=n),
        ),
    )


async def add_outbound(conn, n, room=DM, run_id=None, at=None):
    await conn.execute(
        "INSERT INTO outbound (room_id, run_id, kind, body, txn_id, status, created_at)"
        " VALUES (%s, %s, 'reply', %s, %s, 'sent', %s)",
        (room, run_id, f"answer {n}", f"txn-{n}", at or T0 + timedelta(seconds=n)),
    )


def room_path(room_id: str, suffix: str = "") -> str:
    return f"/rooms/{quote(room_id, safe='')}{suffix}"


async def test_meta(api, conn):
    r = await api.get("/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == BOT
    assert body["homeserver_url"] == "http://hs:8008"
    assert body["default_policy"] == "deny"
    assert body["group_trigger"] == "mention"
    assert body["rule_kinds"] == {
        "allow": ["user", "facebook", "room", "alias"],
        "deny": ["user", "facebook", "room", "alias", "display_name"],
    }
    assert body["sync"] == {"last_sync_at": None, "healthy": False}

    now = datetime.now(UTC)
    await conn.execute("UPDATE sync_state SET last_sync_at = %s", (now - timedelta(seconds=80),))
    sync = (await api.get("/meta")).json()["sync"]
    assert sync["healthy"] is True and sync["last_sync_at"] is not None
    await conn.execute("UPDATE sync_state SET last_sync_at = %s", (now - timedelta(seconds=91),))
    assert (await api.get("/meta")).json()["sync"]["healthy"] is False


async def test_list_rooms(api, conn, fake):
    await seed(conn)
    pid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO room_settings (room_id, preset_id, trigger) VALUES (%s, %s, 'mention')",
        (DM, pid),
    )
    sid = uuid.uuid4()
    await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, slug, session_id, status)"
        " VALUES (%s, %s, 'first', 'mx-1', %s, 'in-progress')",
        (DM, pid, sid),
    )
    await add_inbound(conn, 1, status="new")
    await add_inbound(conn, 2, status="new")

    rooms = (await api.get("/rooms")).json()
    assert [r["room_id"] for r in rooms] == [DM, GRP, ENC, INFRA]
    dm, grp, enc, infra = rooms
    assert dm["name"] == "Alice" and dm["kind"] == "dm" and dm["member_count"] == 2
    assert dm["access"] == {"state": "allowed", "reason": None, "rule_id": 2}
    assert dm["preset_id"] == str(pid)
    assert dm["trigger"] == "mention" and dm["effective_trigger"] == "mention"
    assert dm["session"] == {"id": str(sid), "slug": "mx-1", "status": "in-progress"}
    assert dm["queued"] == 2
    assert dm["last_event_at"] is not None
    assert grp["access"] == {"state": "default", "reason": "not_allowed", "rule_id": None}
    assert grp["trigger"] is None and grp["effective_trigger"] == "mention"
    assert grp["session"] is None and grp["queued"] == 0 and grp["alias"] == "#team:x"
    assert enc["access"]["state"] == "denied" and enc["access"]["reason"] == "room_encrypted"
    assert enc["encrypted"] is True
    assert infra["access"]["reason"] == "room_infrastructure"

    by_kind = (await api.get("/rooms", params={"kind": "group"})).json()
    assert [r["room_id"] for r in by_kind] == [GRP, ENC]
    denied = (await api.get("/rooms", params={"access": "denied"})).json()
    assert [r["room_id"] for r in denied] == [ENC, INFRA]
    allowed = (await api.get("/rooms", params={"access": "allowed"})).json()
    assert [r["room_id"] for r in allowed] == [DM]
    default = (await api.get("/rooms", params={"access": "default", "kind": "group"})).json()
    assert [r["room_id"] for r in default] == [GRP]
    assert (await api.get("/rooms", params={"access": "bogus"})).status_code == 422
    assert (await api.get("/rooms", params={"kind": "bogus"})).status_code == 422


async def test_room_detail(api, conn):
    await seed(conn)
    r = await api.get(room_path(DM))
    assert r.status_code == 200
    body = r.json()
    assert body["room_id"] == DM and body["effective_trigger"] == "all"
    members = {m["user_id"]: m for m in body["members"]}
    assert set(members) == {ALICE, BOB}
    alice = members[ALICE]
    assert alice["display_name"] == "Alice A" and alice["facebook_id"] == "111"
    assert alice["membership"] == "join"
    assert alice["access"] == {"state": "allowed", "reason": None, "rule_id": 2}
    assert members[BOB]["access"] == {"state": "denied", "reason": "denied: user", "rule_id": 1}

    grp = (await api.get(room_path(GRP))).json()
    assert grp["effective_trigger"] == "mention"
    assert {m["user_id"]: m["access"]["state"] for m in grp["members"]} == {
        ALICE: "default",
        BOT: "default",
    }

    missing = await api.get(room_path("!nope:x"))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_room_settings(api, conn, fake):
    await seed(conn)
    preset = fake.add_preset("chat")
    r = await api.put(
        room_path(GRP, "/settings"), json={"preset_id": str(preset.id), "trigger": "all"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["preset_id"] == str(preset.id)
    assert body["trigger"] == "all" and body["effective_trigger"] == "all"
    cur = await conn.execute(
        "SELECT preset_id, trigger FROM room_settings WHERE room_id = %s", (GRP,)
    )
    assert await cur.fetchone() == (preset.id, "all")

    cleared = await api.put(room_path(GRP, "/settings"), json={"preset_id": None, "trigger": None})
    assert cleared.status_code == 200
    assert cleared.json()["preset_id"] is None and cleared.json()["trigger"] is None
    cur = await conn.execute(
        "SELECT preset_id, trigger FROM room_settings WHERE room_id = %s", (GRP,)
    )
    assert await cur.fetchone() == (None, None)

    unknown = await api.put(
        room_path(GRP, "/settings"), json={"preset_id": str(uuid.uuid4()), "trigger": None}
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_preset"

    bad = await api.put(
        room_path(GRP, "/settings"), json={"preset_id": None, "trigger": "sometimes"}
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"

    missing = await api.put(room_path("!nope:x", "/settings"), json={"preset_id": None})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    cur = await conn.execute("SELECT count(*) FROM room_settings WHERE room_id = '!nope:x'")
    assert (await cur.fetchone())[0] == 0


async def test_room_messages(api, conn):
    await seed(conn)
    # newest first: out 6 (t6), in 5, out 4, in 3, in 2, out 1 (t1)
    await add_inbound(conn, 2)
    await add_inbound(conn, 3, status="ignored")
    await add_inbound(conn, 5, room=GRP)
    await add_inbound(conn, 5)
    await add_outbound(conn, 1)
    await add_outbound(conn, 4)
    await add_outbound(conn, 6)
    await add_outbound(conn, 7, room=GRP)

    r = await api.get(room_path(DM, "/messages"))
    assert r.status_code == 200
    msgs = r.json()
    assert [(m["direction"], m["body"]) for m in msgs] == [
        ("out", "answer 6"),
        ("in", "hello 5"),
        ("out", "answer 4"),
        ("in", "hello 3"),
        ("in", "hello 2"),
        ("out", "answer 1"),
    ]
    out, inn = msgs[0], msgs[1]
    assert out["sender"] == BOT and out["status"] == "sent" and out["sender_name"] is None
    assert inn["sender"] == ALICE and inn["sender_name"] == "Alice A" and inn["status"] == "handled"
    assert len({m["id"] for m in msgs}) == 6

    first = (await api.get(room_path(DM, "/messages"), params={"limit": 2})).json()
    assert [m["body"] for m in first] == ["answer 6", "hello 5"]
    second = (
        await api.get(room_path(DM, "/messages"), params={"limit": 2, "before_id": first[-1]["id"]})
    ).json()
    assert [m["body"] for m in second] == ["answer 4", "hello 3"]
    third = (
        await api.get(
            room_path(DM, "/messages"), params={"limit": 2, "before_id": second[-1]["id"]}
        )
    ).json()
    assert [m["body"] for m in third] == ["hello 2", "answer 1"]
    last = (
        await api.get(room_path(DM, "/messages"), params={"limit": 2, "before_id": third[-1]["id"]})
    ).json()
    assert last == []

    assert (await api.get(room_path("!nope:x", "/messages"))).status_code == 404
    bad = await api.get(room_path(DM, "/messages"), params={"before_id": "zzz"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    assert (await api.get(room_path(DM, "/messages"), params={"limit": 0})).status_code == 422


async def test_users(api, conn):
    await seed(conn)
    users = (await api.get("/users")).json()
    assert [u["user_id"] for u in users] == [BOT, ALICE, BOB]
    _, alice, bob = users
    assert alice["display_name"] == "Alice A" and alice["facebook_id"] == "111"
    assert alice["rooms"] == [DM, GRP]
    assert alice["access"] == {"state": "default", "reason": "not_allowed", "rule_id": None}
    assert bob["access"] == {"state": "denied", "reason": "denied: user", "rule_id": 1}

    by_name = (await api.get("/users", params={"q": "bob"})).json()
    assert [u["user_id"] for u in by_name] == [BOB]
    by_fb = (await api.get("/users", params={"q": "111"})).json()
    assert [u["user_id"] for u in by_fb] == [ALICE]
    assert [u["user_id"] for u in (await api.get("/users", params={"limit": 1})).json()] == [BOT]
    assert (await api.get("/users", params={"limit": 0})).status_code == 422

    await conn.execute(
        "INSERT INTO access_rules (list, kind, value) VALUES ('allow', 'facebook', '111')"
    )
    alice = (await api.get("/users", params={"q": "alice"})).json()[0]
    assert alice["access"]["state"] == "allowed"


async def test_cidr_and_error_body(pool, orchestrator, conn):
    app = create_api_app(pool, orchestrator, make_settings())
    outside = httpx.ASGITransport(app=app, client=("10.1.2.3", 5000))
    async with httpx.AsyncClient(transport=outside, base_url="http://api") as c:
        r = await c.get(PREFIX + "/meta")
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"
        assert (await c.get(PREFIX + "/rooms")).status_code == 403
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 200

    wide = create_api_app(pool, orchestrator, make_settings(api_allowed_cidrs=["10.0.0.0/8"]))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wide, client=("10.1.2.3", 5000)),
        base_url="http://api",
    ) as c:
        assert (await c.get(PREFIX + "/meta")).status_code == 200

    inside = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=inside, base_url="http://api") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}
        missing = await c.get(PREFIX + "/nope")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "not_found"
        invalid = await c.get(PREFIX + "/rooms", params={"kind": "bogus"})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_request"
        assert set(invalid.json()["error"]) == {"code", "message"}
