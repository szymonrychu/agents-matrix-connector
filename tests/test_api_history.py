"""History routes: the agent listener (room from the caller's binding) and the admin listener."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import test_api_agent as rig
from api_helpers import DM, GRP, add_inbound, add_outbound, make_settings, room_path, seed
from history_helpers import add_m, add_o, add_s
from test_agent_gate import bearer, make_token

from agents_matrix_connector.api.agent import ADMIN_PREFIX, create_agent_app

agent_for = rig.agent_for
agent = rig.agent
ROOT = Path(__file__).resolve().parent.parent
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
R = "!hist:x"
OTHER = "!other:x"


def t(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
async def admin(pool, orchestrator):
    token, digest = make_token()
    app = create_agent_app(pool, orchestrator, make_settings(agent_token_sha256=digest))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agents" + ADMIN_PREFIX
    ) as client:
        yield client, bearer(token)


async def bound_room(conn, room=R) -> uuid.UUID:
    session = uuid.uuid4()
    await rig.add_room(conn, room)
    await rig.allow_room(conn, room)
    await rig.bind(conn, room, session)
    return session


async def get(agent, session, **params) -> httpx.Response:
    return await agent.get("/history", params=params, headers=rig.as_(session))


async def test_history_needs_a_bound_room(agent, conn):
    r = await get(agent, uuid.uuid4())
    rig.refused(r, 409, "room_unbound")


async def test_history_of_own_room(agent, conn):
    session = await bound_room(conn)
    m1 = await add_m(conn, R, t(1))
    m2 = await add_m(conn, R, t(2), status="ignored", reason="not_mentioned")
    o1 = await add_o(conn, R, t(3))
    s1 = await add_s(conn, R, t(4))
    await add_m(conn, OTHER, t(5))
    r = await get(agent, session)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room_id"] == R
    ids = [m["id"] for m in body["messages"]]
    assert ids == [f"m{m1}", f"m{m2}", f"o{o1}", f"s{s1}"]
    assert all("from" in m for m in body["messages"])


async def test_history_cursors_and_limit(agent, conn):
    session = await bound_room(conn)
    ids = [f"m{await add_m(conn, R, t(n))}" for n in range(1, 7)]
    first = (await get(agent, session, limit=2)).json()
    assert [m["id"] for m in first["messages"]] == ids[-2:]
    older = (await get(agent, session, limit=2, before=ids[-2])).json()
    assert [m["id"] for m in older["messages"]] == ids[-4:-2]
    newer = (await get(agent, session, limit=2, after=ids[1])).json()
    assert [m["id"] for m in newer["messages"]] == ids[2:4]
    rig.refused(await get(agent, session, limit=101), 422, "invalid_request")
    rig.refused(await get(agent, session, limit=0), 422, "invalid_request")
    rig.refused(await get(agent, session, before="zzz"), 422, "bad_reference")
    both = await get(agent, session, before=ids[1], after=ids[0])
    rig.refused(both, 422, "invalid_request")
    assert len((await get(agent, session)).json()["messages"]) == 6


async def test_admin_history_read_is_audited_with_session(admin, conn, caplog):
    client, headers = admin
    await seed(conn)
    await add_m(conn, DM, t(1))
    sid = uuid.uuid4()
    with caplog.at_level("INFO", logger="agents_matrix_connector.api.rooms"):
        r = await client.get(
            f"/rooms/{DM}/messages", headers={**headers, "X-Orch-Session": str(sid)}
        )
        assert r.status_code == 200
        await client.get(
            f"/rooms/{DM}/messages", headers={**headers, "X-Orch-Session": "not-a-uuid"}
        )
    lines = [x.getMessage() for x in caplog.records if "admin history read" in x.getMessage()]
    assert lines[0] == f"admin history read session={sid} room={DM} count=1"
    assert lines[1] == f"admin history read session=None room={DM} count=1"


async def test_history_default_returns_at_most_50(agent, conn):
    session = await bound_room(conn)
    for n in range(55):
        await add_m(conn, R, t(n))
    body = (await get(agent, session)).json()
    assert len(body["messages"]) == 50 and body["has_more_before"] is True


async def test_history_sender_filter(agent, conn):
    session = await bound_room(conn)
    await add_m(conn, R, t(1), name="Alice A")
    await add_m(conn, R, t(2), name="Bob B")
    body = (await get(agent, session, **{"from": "alice a"})).json()
    assert [m["from"] for m in body["messages"]] == ["Alice A"]
    rig.refused(await get(agent, session, **{"from": "x" * 201}), 422, "invalid_request")


async def test_history_unknown_and_foreign_ids_same_body(agent, conn):
    session = await bound_room(conn)
    await add_m(conn, R, t(1))
    foreign = await add_m(conn, OTHER, t(2))
    a = await get(agent, session, before=f"m{foreign}")
    b = await get(agent, session, before="m999999")
    rig.refused(a, 422, "bad_reference")
    rig.refused(b, 422, "bad_reference")
    assert a.json() == b.json()


def test_history_takes_no_room_parameter():
    spec = json.loads((ROOT / "openapi-agent.json").read_text(encoding="utf-8"))
    params = spec["paths"]["/chat/v1/history"]["get"]["parameters"]
    assert {p["name"] for p in params} == {
        "before",
        "after",
        "from",
        "limit",
        "X-Orch-Session",
        "X-Orch-Slug",
    }


async def test_history_needs_the_bearer(agent, conn):
    session = await bound_room(conn)
    r = await agent.get("/history", headers={"X-Orch-Session": str(session)})
    rig.refused(r, 401, "unauthorized")
    r = await agent.get("/history", headers={"Authorization": "Bearer nope"})
    rig.refused(r, 401, "unauthorized")


async def test_history_needs_a_session_header(agent, conn):
    r = await agent.get("/history", headers={"Authorization": f"Bearer {rig.TOKEN}"})
    rig.refused(r, 401, "unauthorized")


async def test_admin_history_any_room(admin, conn):
    client, headers = admin
    await seed(conn)
    m = await add_m(conn, DM, t(1))
    r = await client.get(f"/rooms/{DM}/messages", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room_id"] == DM and [x["id"] for x in body["messages"]] == [f"m{m}"]
    assert (await client.get(f"/rooms/{GRP}/messages", headers=headers)).json()["messages"] == []
    missing = await client.get("/rooms/!nope:x/messages", headers=headers)
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert (await client.get(f"/rooms/{DM}/messages")).status_code == 401
    bad = await client.get(f"/rooms/{DM}/messages", params={"limit": 101}, headers=headers)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_request"
    foreign = await add_m(conn, GRP, t(2))
    ref = await client.get(
        f"/rooms/{DM}/messages", params={"before": f"m{foreign}"}, headers=headers
    )
    assert ref.status_code == 422 and ref.json()["error"]["code"] == "bad_reference"


async def test_ui_messages_route_unchanged(api, conn):
    await seed(conn)
    await add_inbound(conn, 2)
    await add_outbound(conn, 3)
    r = await api.get(room_path(DM, "/messages"))
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()]
    assert len(ids) == 2 and {i.split("-")[0] for i in ids} == {"in", "out"}
