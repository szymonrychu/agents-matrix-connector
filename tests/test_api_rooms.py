import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
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


async def test_room_settings_anonymize_names(api, conn):
    await seed(conn)
    assert (await api.get(room_path(DM))).json()["anonymize_names"] is False
    assert all(r["anonymize_names"] is False for r in (await api.get("/rooms")).json())

    on = await api.put(room_path(DM, "/settings"), json={"anonymize_names": True})
    assert on.status_code == 200 and on.json()["anonymize_names"] is True
    assert (await _listed(api, DM))["anonymize_names"] is True

    kept = await api.put(room_path(DM, "/settings"), json={"trigger": "all"})
    assert kept.json()["anonymize_names"] is True
    null = await api.put(room_path(DM, "/settings"), json={"anonymize_names": None})
    assert null.json()["anonymize_names"] is True

    off = await api.put(room_path(DM, "/settings"), json={"anonymize_names": False})
    assert off.json()["anonymize_names"] is False
    assert (await api.get(room_path(DM))).json()["anonymize_names"] is False

    bad = await api.put(room_path(DM, "/settings"), json={"anonymize_names": "maybe"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"


async def test_put_settings_aliases(api, conn):
    await seed(conn)
    assert (await api.get(room_path(DM))).json()["agent_aliases"] == []
    assert all(r["agent_aliases"] == [] for r in (await api.get("/rooms")).json())

    put = await api.put(
        room_path(DM, "/settings"), json={"agent_aliases": [" @Bot ", "bot", "Agent"]}
    )
    assert put.status_code == 200 and put.json()["agent_aliases"] == ["Bot", "Agent"]
    assert (await api.get(room_path(DM))).json()["agent_aliases"] == ["Bot", "Agent"]
    assert (await _listed(api, DM))["agent_aliases"] == ["Bot", "Agent"]

    kept = await api.put(room_path(DM, "/settings"), json={"trigger": "all"})
    assert kept.json()["agent_aliases"] == ["Bot", "Agent"]

    cleared = await api.put(room_path(DM, "/settings"), json={"agent_aliases": []})
    assert cleared.status_code == 200 and cleared.json()["agent_aliases"] == []


@pytest.mark.parametrize("aliases", [["x" * 65], [""], ["!!!"], [f"alias{i}" for i in range(11)]])
async def test_put_settings_aliases_invalid(api, conn, aliases):
    await seed(conn)
    await api.put(room_path(DM, "/settings"), json={"agent_aliases": ["keep"]})
    bad = await api.put(room_path(DM, "/settings"), json={"agent_aliases": aliases})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    assert (await api.get(room_path(DM))).json()["agent_aliases"] == ["keep"]


async def test_allow_room_aliases(api, conn):
    await seed(conn)
    first = await api.post(room_path(GRP, "/allow"), json={"agent_aliases": ["bot"]})
    assert first.status_code == 200 and first.json()["agent_aliases"] == ["bot"]
    second = await api.post(room_path(GRP, "/allow"), json={})
    assert second.status_code == 200 and second.json()["agent_aliases"] == ["bot"]


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


async def test_room_view_effective_preset_defaults(api, conn):
    await seed(conn)
    default, own = uuid.uuid4(), uuid.uuid4()
    rooms = {r["room_id"]: r for r in (await api.get("/rooms")).json()}
    assert rooms[GRP]["effective_preset_id"] is None
    await conn.execute("UPDATE settings SET default_preset_id = %s", (default,))
    await conn.execute("INSERT INTO room_settings (room_id, preset_id) VALUES (%s, %s)", (DM, own))
    rooms = {r["room_id"]: r for r in (await api.get("/rooms")).json()}
    assert rooms[GRP]["preset_id"] is None
    assert rooms[GRP]["effective_preset_id"] == str(default)
    assert rooms[DM]["effective_preset_id"] == str(own)
    detail = (await api.get(room_path(GRP))).json()
    assert detail["effective_preset_id"] == str(default)


async def test_room_view_pending_block(api, conn):
    await seed(conn)
    await conn.execute("UPDATE rooms SET message_request = true WHERE room_id = %s", (GRP,))
    for n, (sender, name, reason) in enumerate(
        [(ALICE, "Alice A", "not_allowed"), (BOB, "Bob B", "denied: user")]
    ):
        await conn.execute(
            "INSERT INTO inbound (event_id, room_id, sender, sender_name, body, status, reason,"
            " received_at) VALUES (%s, %s, %s, %s, 'secret body', 'ignored', %s, %s)",
            (f"$d{n}", GRP, sender, name, reason, T0 + timedelta(minutes=n)),
        )
    listed = await api.get("/rooms")
    assert "secret body" not in listed.text
    grp = {r["room_id"]: r for r in listed.json()}[GRP]
    assert grp["pending"] == {
        "allowed": False,
        "invited": False,
        "inviter": None,
        "message_request": True,
        "denied_count": 2,
        "last_denied_at": (T0 + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "denied_senders": [
            {"user_id": ALICE, "display_name": "Alice A", "facebook_id": "111"},
            {"user_id": BOB, "display_name": "Bob B", "facebook_id": "222"},
        ],
    }
    dm = {r["room_id"]: r for r in listed.json()}[DM]
    assert dm["pending"]["allowed"] is True and dm["pending"]["denied_count"] == 0
    assert dm["pending"]["denied_senders"] == [] and dm["pending"]["last_denied_at"] is None
    detail = await api.get(room_path(GRP))
    assert detail.json()["pending"]["denied_count"] == 2
    assert "secret body" not in detail.text


async def test_room_view_invite_pending(api, conn):
    await conn.execute(
        "INSERT INTO rooms (room_id, membership, inviter) VALUES ('!inv:x', 'invite', %s)",
        ("@facebookbot:x",),
    )
    room = (await api.get(room_path("!inv:x"))).json()
    assert room["membership"] == "invite"
    assert room["pending"]["invited"] is True
    assert room["pending"]["inviter"] == "@facebookbot:x"
    assert room["pending"]["allowed"] is False


async def _rule_ids(conn, room_id):
    cur = await conn.execute(
        "SELECT id FROM access_rules WHERE list = 'allow' AND kind = 'room' AND value = %s",
        (room_id,),
    )
    return [row[0] for row in await cur.fetchall()]


async def _accept_rows(conn, room_id):
    cur = await conn.execute(
        "SELECT id, status, action FROM room_requests WHERE room_id = %s ORDER BY id", (room_id,)
    )
    return await cur.fetchall()


async def test_allow_creates_rule_and_settings(api, conn, fake):
    await seed(conn)
    preset = fake.add_preset("chat")
    r = await api.post(
        room_path(GRP, "/allow"),
        json={"preset_id": str(preset.id), "trigger": "all", "agents_may_initiate": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["access"]["state"] == "allowed"
    assert body["pending"]["allowed"] is True
    assert body["preset_id"] == str(preset.id) and body["effective_preset_id"] == str(preset.id)
    assert body["trigger"] == "all" and body["agents_may_initiate"] is True
    assert body["members"]
    assert body["rule_id"] == (await _rule_ids(conn, GRP))[0]
    assert body["accept_request_id"] is None
    cur = await conn.execute("SELECT note FROM access_rules WHERE id = %s", (body["rule_id"],))
    assert (await cur.fetchone())[0] == "allowed via api"


async def test_allow_is_idempotent(api, conn):
    await seed(conn)
    first = (await api.post(room_path(GRP, "/allow"), json={})).json()
    second = await api.post(room_path(GRP, "/allow"), json={"trigger": "mention"})
    assert second.status_code == 200
    assert second.json()["rule_id"] == first["rule_id"]
    assert await _rule_ids(conn, GRP) == [first["rule_id"]]
    assert second.json()["trigger"] == "mention"


async def test_allow_message_request_queues_accept(api, conn):
    await seed(conn)
    await conn.execute("UPDATE rooms SET message_request = true WHERE room_id = %s", (GRP,))
    r = await api.post(room_path(GRP, "/allow"), json={})
    accept = r.json()["accept_request_id"]
    assert accept is not None
    assert await _accept_rows(conn, GRP) == [(accept, "confirmed", "accept")]
    again = await api.post(room_path(GRP, "/allow"), json={})
    assert again.json()["accept_request_id"] == accept
    assert len(await _accept_rows(conn, GRP)) == 1


async def test_allow_invite_room_queues_accept(api, conn):
    await conn.execute(
        "INSERT INTO rooms (room_id, membership, inviter) VALUES ('!inv:x', 'invite', '@b:x')"
    )
    r = await api.post(
        room_path("!inv:x", "/allow"),
        json={},
        headers={"X-Orch-Session": "8c5f2b3e-8a7e-4a0e-9a59-1d1b7c6f0a11", "X-Orch-Slug": "hive-1"},
    )
    assert r.status_code == 200
    assert r.json()["accept_request_id"] is not None
    cur = await conn.execute(
        "SELECT requested_by::text, requested_slug FROM room_requests WHERE room_id = '!inv:x'"
    )
    assert await cur.fetchone() == ("8c5f2b3e-8a7e-4a0e-9a59-1d1b7c6f0a11", "hive-1")
    cur = await conn.execute("SELECT note FROM access_rules WHERE id = %s", (r.json()["rule_id"],))
    assert (await cur.fetchone())[0] == "allowed by agent hive-1"


async def test_allow_plain_room_no_accept(api, conn):
    await seed(conn)
    r = await api.post(room_path(GRP, "/allow"), json={})
    assert r.json()["accept_request_id"] is None
    assert await _accept_rows(conn, GRP) == []


async def test_allow_unknown_room_404(api, conn):
    r = await api.post(room_path("!nope:x", "/allow"), json={})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert await _rule_ids(conn, "!nope:x") == []


async def test_allow_unknown_preset_422(api, conn):
    await seed(conn)
    r = await api.post(room_path(GRP, "/allow"), json={"preset_id": str(uuid.uuid4())})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_preset"
    assert await _rule_ids(conn, GRP) == []
    cur = await conn.execute("SELECT count(*) FROM room_settings WHERE room_id = %s", (GRP,))
    assert (await cur.fetchone())[0] == 0


async def test_allow_room_version_12_id_without_server(api, conn):
    room_id = "!OBcFGV30jyOrJmTuKqlh-rHGj8vXBVczfgjZQWif_Mw"
    await conn.execute("INSERT INTO rooms (room_id, membership) VALUES (%s, 'join')", (room_id,))
    r = await api.post(room_path(room_id, "/allow"), json={})
    assert r.status_code == 200
    assert len(await _rule_ids(conn, room_id)) == 1


async def test_put_settings_send_files_flag(api, conn):
    await seed(conn)
    assert (await api.get(room_path(DM))).json()["agents_may_send_files"] is False
    assert (await _listed(api, DM))["agents_may_send_files"] is False

    on = await api.put(room_path(DM, "/settings"), json={"agents_may_send_files": True})
    assert on.status_code == 200 and on.json()["agents_may_send_files"] is True
    assert (await _listed(api, DM))["agents_may_send_files"] is True

    other = await api.put(room_path(DM, "/settings"), json={"trigger": "all"})
    assert other.json()["agents_may_send_files"] is True
    null = await api.put(room_path(DM, "/settings"), json={"agents_may_send_files": None})
    assert null.json()["agents_may_send_files"] is True

    off = await api.put(room_path(DM, "/settings"), json={"agents_may_send_files": False})
    assert off.json()["agents_may_send_files"] is False
    cur = await conn.execute(
        "SELECT agents_may_send_files FROM room_settings WHERE room_id = %s", (DM,)
    )
    assert (await cur.fetchone())[0] is False
