import uuid
from datetime import UTC, datetime, timedelta

import httpx
from api_helpers import (
    ALICE,
    BOB,
    BOT,
    DM,
    ENC,
    GRP,
    INFRA,
    PREFIX,
    T0,
    add_inbound,
    add_outbound,
    make_settings,
    room_path,
    seed,
)

from agents_matrix_connector.api.app import create_api_app


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


async def _stored_flag(conn, room_id):
    cur = await conn.execute(
        "SELECT agents_may_initiate FROM room_settings WHERE room_id = %s", (room_id,)
    )
    row = await cur.fetchone()
    return None if row is None else row[0]


async def _listed(api, room_id):
    return next(r for r in (await api.get("/rooms")).json() if r["room_id"] == room_id)


def _binding(room):
    binding = room["binding"]
    return binding and {**binding, "since": datetime.fromisoformat(binding["since"])}


async def test_room_settings_agents_may_initiate(api, conn):
    await seed(conn)
    r = await api.put(room_path(DM, "/settings"), json={"agents_may_initiate": True})
    assert r.status_code == 200
    assert r.json()["agents_may_initiate"] is True
    assert await _stored_flag(conn, DM) is True

    assert (await api.get(room_path(DM))).json()["agents_may_initiate"] is True
    rooms = {r["room_id"]: r["agents_may_initiate"] for r in (await api.get("/rooms")).json()}
    assert rooms == {DM: True, GRP: False, ENC: False, INFRA: False}


async def test_room_settings_flag_omitted_keeps_value(api, conn, fake):
    await seed(conn)
    preset = fake.add_preset("chat")
    assert (
        await api.put(room_path(DM, "/settings"), json={"agents_may_initiate": True})
    ).status_code == 200

    kept = await api.put(
        room_path(DM, "/settings"), json={"preset_id": str(preset.id), "trigger": "all"}
    )
    assert kept.status_code == 200
    body = kept.json()
    assert body["agents_may_initiate"] is True
    assert body["preset_id"] == str(preset.id) and body["trigger"] == "all"
    assert await _stored_flag(conn, DM) is True

    null = await api.put(room_path(DM, "/settings"), json={"agents_may_initiate": None})
    assert null.json()["agents_may_initiate"] is True
    assert null.json()["preset_id"] is None and null.json()["trigger"] is None
    assert await _stored_flag(conn, DM) is True

    cleared = await api.put(room_path(DM, "/settings"), json={"agents_may_initiate": False})
    assert cleared.status_code == 200 and cleared.json()["agents_may_initiate"] is False
    assert await _stored_flag(conn, DM) is False

    still = await api.put(room_path(DM, "/settings"), json={"trigger": "mention"})
    assert still.json()["agents_may_initiate"] is False
    assert await _stored_flag(conn, DM) is False
    assert (await _listed(api, DM))["agents_may_initiate"] is False

    bad = await api.put(room_path(DM, "/settings"), json={"agents_may_initiate": "maybe"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    assert await _stored_flag(conn, DM) is False


async def test_agents_may_initiate_default_false(api, conn):
    await seed(conn)
    assert await _stored_flag(conn, GRP) is None
    assert (await api.get(room_path(GRP))).json()["agents_may_initiate"] is False
    assert all(r["agents_may_initiate"] is False for r in (await api.get("/rooms")).json())

    saved = await api.put(room_path(GRP, "/settings"), json={"trigger": "all"})
    assert saved.status_code == 200 and saved.json()["agents_may_initiate"] is False
    assert await _stored_flag(conn, GRP) is False


async def test_room_binding_view(api, conn):
    await seed(conn)
    bound, older, latest = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cur = await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, slug, session_id, status)"
        " VALUES (%s, %s, 'first', 'mx-1', %s, 'handled') RETURNING id",
        (DM, uuid.uuid4(), bound),
    )
    dm_run = (await cur.fetchone())[0]
    since = T0 + timedelta(minutes=5)
    await conn.execute(
        "INSERT INTO room_bindings (room_id, session_id, slug, origin, run_id, created_at)"
        " VALUES (%s, %s, 'mx-1', 'inbound', %s, %s)",
        (DM, bound, dm_run, since),
    )
    # The latest run differs from the binding: `session` stays the latest run.
    await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, slug, session_id, status)"
        " VALUES (%s, NULL, 'follow', 'mx-1', %s, 'in-progress')",
        (DM, bound),
    )

    cur = await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, status)"
        " VALUES (%s, %s, 'first', 'in-progress') RETURNING id",
        (GRP, uuid.uuid4()),
    )
    grp_run = (await cur.fetchone())[0]
    await conn.execute(
        "INSERT INTO room_bindings (room_id, origin, run_id, created_at)"
        " VALUES (%s, 'inbound', %s, %s)",
        (GRP, grp_run, since + timedelta(minutes=1)),
    )

    await conn.execute(
        "INSERT INTO room_bindings (room_id, session_id, slug, origin, send_id, released_at,"
        " release_reason) VALUES (%s, %s, 'hive', 'agent', 7, now(), 'session_gone')",
        (ENC, older),
    )
    await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, slug, session_id, status)"
        " VALUES (%s, %s, 'first', 'mx-9', %s, 'failed')",
        (ENC, uuid.uuid4(), latest),
    )

    expected_dm = {
        "session_id": str(bound),
        "slug": "mx-1",
        "origin": "inbound",
        "since": since,
    }
    expected_grp = {
        "session_id": None,
        "slug": None,
        "origin": "inbound",
        "since": since + timedelta(minutes=1),
    }
    rooms = {r["room_id"]: r for r in (await api.get("/rooms")).json()}
    details = {rid: (await api.get(room_path(rid))).json() for rid in (DM, GRP, ENC, INFRA)}
    for view in (rooms, details):
        assert _binding(view[DM]) == expected_dm
        assert view[DM]["session"] == {"id": str(bound), "slug": "mx-1", "status": "in-progress"}
        assert _binding(view[GRP]) == expected_grp
        assert view[GRP]["session"] == {"id": None, "slug": None, "status": "in-progress"}
        assert view[ENC]["binding"] is None
        assert view[ENC]["session"] == {"id": str(latest), "slug": "mx-9", "status": "failed"}
        assert view[INFRA]["binding"] is None and view[INFRA]["session"] is None

    saved = await api.put(room_path(DM, "/settings"), json={"trigger": "all"})
    assert _binding(saved.json()) == expected_dm


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
