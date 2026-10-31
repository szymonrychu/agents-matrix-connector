"""The admin surface on the token-gated agent listener: rules and rooms for the superleader."""

import httpx
import pytest
from api_helpers import DM, make_settings, seed
from test_agent_gate import bearer, make_token

from agents_matrix_connector.api.agent import ADMIN_PREFIX, CHAT_PREFIX, create_agent_app

ROOM_X = "!x:szymonrichert.pl"


@pytest.fixture
async def admin(pool, orchestrator):
    token, digest = make_token()
    app = create_agent_app(pool, orchestrator, make_settings(agent_token_sha256=digest))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agents" + ADMIN_PREFIX
    ) as client:
        yield client, bearer(token)


def test_admin_prefix_is_under_chat():
    assert ADMIN_PREFIX == CHAT_PREFIX + "/admin"


async def test_admin_routes_need_the_bearer(admin):
    client, _ = admin
    calls = [
        ("GET", "/rooms", None),
        ("PUT", f"/rooms/{DM}/settings", {"trigger": "all"}),
        ("GET", "/rules", None),
        ("POST", "/rules", {"list": "allow", "kind": "room", "value": ROOM_X}),
        ("DELETE", "/rules/1", None),
    ]
    for method, path, body in calls:
        r = await client.request(method, path, json=body)
        assert r.status_code == 401, (method, path)
        assert r.json()["error"]["code"] == "unauthorized"
        wrong = await client.request(
            method, path, json=body, headers={"Authorization": "Bearer nope"}
        )
        assert wrong.status_code == 401, (method, path)


async def test_rules_and_rooms_with_the_bearer(admin, conn):
    client, headers = admin
    await seed(conn)
    created = await client.post(
        "/rules", json={"list": "allow", "kind": "room", "value": ROOM_X}, headers=headers
    )
    assert created.status_code == 201
    rule_id = created.json()["id"]
    listed = await client.get("/rules", headers=headers)
    assert rule_id in [r["id"] for r in listed.json()["allow"]]
    assert (await client.delete(f"/rules/{rule_id}", headers=headers)).status_code == 204
    after = await client.get("/rules", headers=headers)
    assert rule_id not in [r["id"] for r in after.json()["allow"]]

    rooms = await client.get("/rooms", headers=headers)
    assert rooms.status_code == 200 and DM in {r["room_id"] for r in rooms.json()}
    put = await client.put(
        f"/rooms/{DM}/settings",
        json={"trigger": "all", "agents_may_initiate": True},
        headers=headers,
    )
    assert put.status_code == 200
    assert put.json()["trigger"] == "all" and put.json()["agents_may_initiate"] is True
    missing = await client.put("/rooms/!nope:x/settings", json={}, headers=headers)
    assert missing.status_code == 404


async def test_unknown_preset_goes_through_the_orchestrator(admin, conn):
    client, headers = admin
    await seed(conn)
    r = await client.put(
        f"/rooms/{DM}/settings",
        json={"preset_id": "00000000-0000-0000-0000-000000000001"},
        headers=headers,
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_preset"


async def test_only_the_five_admin_routes_are_served(admin):
    client, headers = admin
    for method, path in [
        ("PATCH", "/rules/1"),
        ("GET", "/settings"),
        ("PUT", "/settings"),
        ("GET", "/users"),
        ("GET", "/messages"),
        ("GET", f"/rooms/{DM}"),
        ("GET", f"/rooms/{DM}/messages"),
        ("GET", "/meta"),
    ]:
        r = await client.request(method, path, headers=headers)
        assert r.status_code in (404, 405), (method, path, r.status_code)
