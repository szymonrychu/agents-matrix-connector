# ruff: noqa: F811
import uuid

from test_api_rooms import (
    ALICE,
    BOB,
    DM,
    ENC,
    GRP,
    add_inbound,
    api,  # noqa: F401
    conn,  # noqa: F401
    fake,  # noqa: F401
    orchestrator,  # noqa: F401
    pool,  # noqa: F401
    seed,
    url,  # noqa: F401
)

ROOM_X = "!x:szymonrichert.pl"


async def test_rules_crud_changes_access(api, conn):
    await conn.execute(
        "INSERT INTO rooms (room_id, kind, name, membership, member_count)"
        " VALUES (%s, 'group', 'X', 'join', 3)",
        (ROOM_X,),
    )
    assert (await api.get("/rules")).json() == {"allow": [], "deny": []}
    before = (await api.get("/rooms")).json()
    assert before[0]["access"]["state"] == "default"

    created = await api.post("/rules", json={"list": "allow", "kind": "room", "value": ROOM_X})
    assert created.status_code == 201
    rule = created.json()
    assert set(rule) == {"id", "list", "kind", "value", "note", "created_at"}
    assert rule["list"] == "allow" and rule["value"] == ROOM_X and rule["note"] is None
    access = (await api.get("/rooms")).json()[0]["access"]
    assert access == {"state": "allowed", "reason": None, "rule_id": rule["id"]}

    noted = await api.post(
        "/rules", json={"list": "deny", "kind": "user", "value": BOB, "note": "spam"}
    )
    assert noted.status_code == 201 and noted.json()["note"] == "spam"
    listed = (await api.get("/rules")).json()
    assert [r["id"] for r in listed["allow"]] == [rule["id"]]
    assert [r["value"] for r in listed["deny"]] == [BOB]

    assert (await api.delete(f"/rules/{rule['id']}")).status_code == 204
    assert (await api.get("/rules")).json()["allow"] == []
    assert (await api.get("/rooms")).json()[0]["access"]["state"] == "default"


async def test_invalid_rules(api):
    bad_list = await api.post("/rules", json={"list": "maybe", "kind": "room", "value": ROOM_X})
    assert bad_list.status_code == 422
    assert bad_list.json()["error"]["code"] == "invalid_rule"
    assert bad_list.json()["error"]["message"].startswith("list:")

    bad_value = await api.post("/rules", json={"list": "allow", "kind": "user", "value": "alice"})
    assert bad_value.status_code == 422
    assert bad_value.json()["error"]["code"] == "invalid_rule"
    assert bad_value.json()["error"]["message"].startswith("value:")

    display = await api.post(
        "/rules", json={"list": "allow", "kind": "display_name", "value": "Alice"}
    )
    assert display.status_code == 422
    assert display.json()["error"]["code"] == "invalid_rule"
    assert "display_name cannot be an allow rule" in display.json()["error"]["message"]

    ok = await api.post("/rules", json={"list": "deny", "kind": "display_name", "value": "Alice"})
    assert ok.status_code == 201

    missing = await api.post("/rules", json={"list": "allow", "kind": "room"})
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "invalid_request"


async def test_duplicate_and_unknown(api):
    body = {"list": "allow", "kind": "user", "value": ALICE}
    assert (await api.post("/rules", json=body)).status_code == 201
    dup = await api.post("/rules", json=body)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "rule_exists"

    gone = await api.delete("/rules/99999")
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "not_found"


async def test_default_preset_setting(api, fake, conn):
    assert (await api.get("/settings")).json() == {"default_preset_id": None}
    preset = fake.add_preset("chat")
    put = await api.put("/settings", json={"default_preset_id": str(preset.id)})
    assert put.status_code == 200
    assert put.json() == {"default_preset_id": str(preset.id)}
    assert (await api.get("/settings")).json() == {"default_preset_id": str(preset.id)}

    unknown = await api.put("/settings", json={"default_preset_id": str(uuid.uuid4())})
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "unknown_preset"
    assert (await api.get("/settings")).json() == {"default_preset_id": str(preset.id)}

    cleared = await api.put("/settings", json={"default_preset_id": None})
    assert cleared.json() == {"default_preset_id": None}
    assert (await api.get("/settings")).json() == {"default_preset_id": None}


async def test_global_messages(api, conn):
    await seed(conn)
    await add_inbound(conn, 1, DM, status="handled")
    await add_inbound(conn, 2, GRP, status="ignored")
    await add_inbound(conn, 3, DM, status="ignored")
    await add_inbound(conn, 4, ENC, status="handled")

    everything = (await api.get("/messages")).json()
    assert [m["body"] for m in everything] == ["hello 4", "hello 3", "hello 2", "hello 1"]
    assert all(m["direction"] == "in" for m in everything)

    ignored = (await api.get("/messages", params={"status": "ignored"})).json()
    assert [m["body"] for m in ignored] == ["hello 3", "hello 2"]

    dm = (await api.get("/messages", params={"room_id": DM})).json()
    assert [m["body"] for m in dm] == ["hello 3", "hello 1"]

    both = (await api.get("/messages", params={"room_id": DM, "status": "ignored"})).json()
    assert [m["body"] for m in both] == ["hello 3"]

    page1 = (await api.get("/messages", params={"limit": 2})).json()
    assert [m["body"] for m in page1] == ["hello 4", "hello 3"]
    page2 = (await api.get("/messages", params={"limit": 2, "before_id": page1[-1]["id"]})).json()
    assert [m["body"] for m in page2] == ["hello 2", "hello 1"]

    bad = await api.get("/messages", params={"before_id": "nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    assert (await api.get("/messages", params={"limit": 0})).status_code == 422
    assert (await api.get("/messages", params={"status": "bogus"})).status_code == 422
