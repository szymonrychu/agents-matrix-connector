"""Room requests on the agent listener: admin create and read, internal decision and confirm."""

import httpx
import pytest
from api_helpers import ALICE, BOT, GRP, make_settings, seed
from test_agent_gate import bearer, make_token

from agents_matrix_connector.api.agent import (
    ADMIN_PREFIX,
    CHAT_PREFIX,
    INTERNAL_PREFIX,
    create_agent_app,
)

MXROOM = "!mx:x"
SESSION = "8c5f2b3e-8a7e-4a0e-9a59-1d1b7c6f0a11"


@pytest.fixture
def settings_extra():
    return {}


@pytest.fixture
async def agent(pool, orchestrator, settings_extra):
    token, digest = make_token()
    app = create_agent_app(
        pool, orchestrator, make_settings(agent_token_sha256=digest, **settings_extra)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agents"
    ) as client:
        client.headers.update(bearer(token))
        yield client


async def create(agent, target, **body):
    return await agent.post(
        ADMIN_PREFIX + "/room-requests",
        json={"target": target, "task_key": "T-25", **body},
        headers={"X-Orch-Session": SESSION, "X-Orch-Slug": "hive-1"},
    )


async def row_count(conn):
    cur = await conn.execute("SELECT count(*) FROM room_requests")
    return (await cur.fetchone())[0]


def test_internal_prefix():
    assert INTERNAL_PREFIX == CHAT_PREFIX + "/internal"


async def test_open_by_facebook_id_pending_confirmation(agent, conn):
    await seed(conn)
    r = await create(agent, "111")
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending_confirmation"
    assert body["action"] == "open"
    assert (body["target_kind"], body["target"], body["display_name"]) == (
        "facebook",
        "111",
        "Alice A",
    )
    assert body["task_key"] == "T-25" and body["decision_id"] is None
    assert body["candidates"] is None and body["room_id"] is None
    cur = await conn.execute(
        "SELECT requested_by::text, requested_slug FROM room_requests WHERE id = %s", (body["id"],)
    )
    assert await cur.fetchone() == (SESSION, "hive-1")


async def test_open_by_matrix_id(agent, conn):
    r = await create(agent, "@carol:example.org")
    body = r.json()
    assert (body["status"], body["target_kind"], body["target"], body["display_name"]) == (
        "pending_confirmation",
        "matrix",
        "@carol:example.org",
        None,
    )


async def test_open_by_profile_link(agent, conn):
    r = await create(agent, "https://m.facebook.com/profile.php?id=4242&ref=x")
    assert r.status_code == 201
    assert (r.json()["target_kind"], r.json()["target"]) == ("facebook", "4242")


async def test_open_by_unique_local_name(agent, conn):
    await seed(conn)
    r = await create(agent, "alice a")
    body = r.json()
    assert body["status"] == "pending_confirmation"
    assert (body["target_kind"], body["target"], body["display_name"]) == (
        "facebook",
        "111",
        "Alice A",
    )


async def test_open_by_unique_local_name_without_facebook_id(agent, conn):
    await conn.execute(
        "INSERT INTO users (user_id, display_name) VALUES ('@carol:example.org', 'Carol')"
    )
    body = (await create(agent, "Carol")).json()
    assert (body["status"], body["target_kind"], body["target"]) == (
        "pending_confirmation",
        "matrix",
        "@carol:example.org",
    )


async def test_open_by_ambiguous_local_name_candidates(agent, conn):
    await seed(conn)
    await conn.execute(
        "INSERT INTO users (user_id, display_name, facebook_id)"
        " VALUES ('@facebook_333:x', 'Alice A', '333')"
    )
    body = (await create(agent, "Alice A")).json()
    assert body["status"] == "candidates"
    assert body["target_kind"] == "name" and body["target"] == "Alice A"
    assert body["candidates"] == [
        {"user_id": ALICE, "display_name": "Alice A", "facebook_id": "111"},
        {"user_id": "@facebook_333:x", "display_name": "Alice A", "facebook_id": "333"},
    ]


async def test_open_name_never_matches_the_bot(agent, conn):
    await seed(conn)
    body = (await create(agent, "Agents")).json()
    assert body["status"] == "resolving"


async def test_open_by_unknown_name_resolving(agent, conn):
    body = (await create(agent, "Nobody Known")).json()
    assert body["status"] == "resolving"
    assert (body["target_kind"], body["target"]) == ("name", "Nobody Known")


async def test_vanity_link_422_no_row(agent, conn):
    r = await create(agent, "https://facebook.com/john.smith")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "vanity_link_unresolvable"
    assert "profile.php?id=" in r.json()["error"]["message"]
    assert await row_count(conn) == 0


@pytest.mark.parametrize(
    "target", ["", "   ", BOT, "@facebookbot:szymonrichert.pl", "https://x.org/a"]
)
async def test_invalid_target_422(agent, conn, target):
    r = await create(agent, target)
    assert r.status_code == 422
    assert r.json()["error"]["code"] in ("invalid_target", "invalid_request")
    assert await row_count(conn) == 0


async def test_bad_task_key_422(agent, conn):
    r = await create(agent, "111", task_key="25")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
    r = await agent.post(ADMIN_PREFIX + "/room-requests", json={"target": "111"})
    assert r.status_code == 422
    assert await row_count(conn) == 0


@pytest.mark.parametrize("settings_extra", [{"room_requests_per_hour": 1}])
async def test_rate_cap_429(agent, conn):
    assert (await create(agent, "https://facebook.com/vanity")).status_code == 422
    assert (await create(agent, "111")).status_code == 201
    r = await create(agent, "222")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert await row_count(conn) == 1


async def test_invite_needs_joined_matrix_room(agent, conn):
    await seed(conn)
    await conn.execute(
        "INSERT INTO rooms (room_id, kind, membership) VALUES (%s, 'matrix', 'join')", (MXROOM,)
    )
    await conn.execute(
        "INSERT INTO rooms (room_id, kind, membership) VALUES ('!inv:x', 'matrix', 'invite')"
    )
    cases = [
        {"target": "@carol:example.org", "action": "invite"},
        {"target": "@carol:example.org", "action": "invite", "room_id": "!nope:x"},
        {"target": "@carol:example.org", "action": "invite", "room_id": "!inv:x"},
        {"target": "@carol:example.org", "action": "invite", "room_id": GRP},
        {"target": "111", "action": "invite", "room_id": MXROOM},
        {"target": "Somebody", "action": "invite", "room_id": MXROOM},
    ]
    for body in cases:
        r = await create(agent, **body)
        assert r.status_code == 422, body
        assert r.json()["error"]["code"] == "invite_not_possible", body
    assert await row_count(conn) == 0
    ok = await create(agent, "@carol:example.org", action="invite", room_id=MXROOM)
    assert ok.status_code == 201
    body = ok.json()
    assert (body["action"], body["status"], body["room_id"], body["target_kind"]) == (
        "invite",
        "pending_confirmation",
        MXROOM,
        "matrix",
    )


async def test_get_room_request_404(agent, conn):
    created = (await create(agent, "111")).json()
    got = await agent.get(f"{ADMIN_PREFIX}/room-requests/{created['id']}")
    assert got.status_code == 200 and got.json() == created
    missing = await agent.get(f"{ADMIN_PREFIX}/room-requests/999999")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"


async def attach(agent, request_id, decision_id, task_key="T-25"):
    return await agent.put(
        f"{INTERNAL_PREFIX}/room-requests/{request_id}/decision",
        json={"task_key": task_key, "decision_id": decision_id},
    )


async def confirm(agent, request_id, decision_id, approved):
    return await agent.post(
        f"{INTERNAL_PREFIX}/room-requests/{request_id}/confirm",
        json={"decision_id": decision_id, "approved": approved},
    )


async def test_internal_decision_then_confirm_approved(agent, conn):
    created = (await create(agent, "111")).json()
    r = await attach(agent, created["id"], 41)
    assert r.status_code == 200 and r.json()["decision_id"] == 41
    again = await attach(agent, created["id"], 42)
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_pending"
    done = await confirm(agent, created["id"], 41, True)
    assert done.status_code == 200 and done.json()["status"] == "confirmed"
    replay = await confirm(agent, created["id"], 41, False)
    assert replay.status_code == 409
    assert (await attach(agent, 999999, 1)).status_code == 404
    assert (await confirm(agent, 999999, 1, True)).status_code == 404


async def test_internal_confirm_wrong_decision_409(agent, conn):
    created = (await create(agent, "111")).json()
    no_decision = await confirm(agent, created["id"], 41, True)
    assert no_decision.status_code == 409 and no_decision.json()["error"]["code"] == "not_pending"
    await attach(agent, created["id"], 41)
    wrong = await confirm(agent, created["id"], 40, True)
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "not_pending"
    row = (await agent.get(f"{ADMIN_PREFIX}/room-requests/{created['id']}")).json()
    assert row["status"] == "pending_confirmation"


async def test_internal_confirm_declined_rejected(agent, conn):
    created = (await create(agent, "111")).json()
    await attach(agent, created["id"], 5)
    r = await confirm(agent, created["id"], 5, False)
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["reason"]) == ("rejected", "user_declined")


async def test_internal_decision_on_resolving_409(agent, conn):
    created = (await create(agent, "Nobody Known")).json()
    r = await attach(agent, created["id"], 5)
    assert r.status_code == 409


async def test_internal_routes_not_under_admin(agent, conn):
    created = (await create(agent, "111")).json()
    for method, path, body in [
        ("POST", f"/room-requests/{created['id']}/confirm", {"decision_id": 1, "approved": True}),
        ("PUT", f"/room-requests/{created['id']}/decision", {"task_key": "T-1", "decision_id": 1}),
    ]:
        r = await agent.request(method, ADMIN_PREFIX + path, json=body)
        assert r.status_code in (404, 405), path


async def test_internal_routes_need_bearer(pool, orchestrator):
    _, digest = make_token()
    app = create_agent_app(pool, orchestrator, make_settings(agent_token_sha256=digest))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agents"
    ) as client:
        for method, path, body in [
            ("POST", f"{INTERNAL_PREFIX}/room-requests/1/confirm", {}),
            ("PUT", f"{INTERNAL_PREFIX}/room-requests/1/decision", {}),
            ("POST", f"{ADMIN_PREFIX}/room-requests", {}),
            ("GET", f"{ADMIN_PREFIX}/room-requests/1", None),
        ]:
            r = await client.request(method, path, json=body)
            assert r.status_code == 401, path
