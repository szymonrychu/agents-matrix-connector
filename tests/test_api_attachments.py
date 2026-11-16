"""Attachment requests: the agent listener's POST and GET /chat/v1/attachments."""

import uuid
from datetime import UTC, datetime

import pytest
import test_api_agent as rig
from history_helpers import add_m

from agents_matrix_connector.db import attachment_requests as repo

agent_for = rig.agent_for
agent = rig.agent
R = "!att:x"
OTHER = "!other:x"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
MEDIA = ("image", "image/png", 10, "cat.png")


async def bound(conn, room=R) -> uuid.UUID:
    session = uuid.uuid4()
    await rig.add_room(conn, room)
    await rig.allow_room(conn, room)
    await rig.bind(conn, room, session)
    return session


async def fetch(agent, session, message) -> object:
    return await agent.post("/attachments", json={"id": message}, headers=rig.as_(session))


async def count(conn) -> int:
    return (await (await conn.execute("SELECT count(*) FROM attachment_requests")).fetchone())[0]


async def test_fetch_queues_and_status_reads_it(agent, conn):
    session = await bound(conn)
    m = await add_m(conn, R, NOW, media=MEDIA)
    r = await fetch(agent, session, f"m{m}")
    assert r.status_code == 202, r.text
    assert r.json() == {"id": "a1", "status": "queued"}
    s = await agent.get("/attachments/a1", headers=rig.as_(session))
    assert s.status_code == 200, s.text
    body = s.json()
    assert (body["id"], body["message"], body["status"], body["reason"]) == (
        "a1",
        f"m{m}",
        "queued",
        None,
    )
    assert body["done_at"] is None and "mxc" not in s.text
    row = await repo.get(conn, 1)
    assert (row.session_id, row.slug, row.room_id, row.inbound_id) == (session, "hive-1", R, m)


async def test_fetch_unbound_session(agent, conn):
    m = await add_m(conn, R, NOW, media=MEDIA)
    rig.refused(await fetch(agent, uuid.uuid4(), f"m{m}"), 409, "room_unbound")


async def test_fetch_unknown_and_foreign_same_body(agent, conn):
    session = await bound(conn)
    foreign = await add_m(conn, OTHER, NOW, media=MEDIA)
    bodies = []
    for ref in ("m999999", f"m{foreign}", "s1", "o1", "x", "m0", "a1"):
        r = await fetch(agent, session, ref)
        rig.refused(r, 422, "bad_reference")
        bodies.append(r.json())
    assert all(b == bodies[0] for b in bodies)
    assert str(foreign) not in bodies[0]["error"]["message"]
    assert await count(conn) == 0


async def test_fetch_without_attachment(agent, conn):
    session = await bound(conn)
    text = await add_m(conn, R, NOW)
    redacted = await add_m(conn, R, NOW, media=MEDIA)
    await conn.execute("UPDATE inbound SET redacted = true WHERE id = %s", (redacted,))
    nourl = await add_m(conn, R, NOW, media=MEDIA)
    await conn.execute("UPDATE inbound SET media_url = NULL WHERE id = %s", (nourl,))
    for m in (text, redacted, nourl):
        rig.refused(await fetch(agent, session, f"m{m}"), 409, "no_attachment")
    assert await count(conn) == 0


WITHHELD_REASONS = [
    "not_allowed",
    "denied: user",
    "room_encrypted",
    "room_infrastructure",
    "unsupported_content",
]


@pytest.mark.parametrize("reason", WITHHELD_REASONS)
async def test_fetch_withheld_row_looks_like_no_attachment(agent, conn, reason):
    session = await bound(conn)
    text = await add_m(conn, R, NOW)
    withheld = await add_m(conn, R, NOW, media=MEDIA, status="ignored", reason=reason)
    plain = await fetch(agent, session, f"m{text}")
    r = await fetch(agent, session, f"m{withheld}")
    rig.refused(r, 409, "no_attachment")
    assert r.json() == plain.json()
    assert await count(conn) == 0


@pytest.mark.parametrize("kind", ["video", "audio"])
async def test_fetch_video_refused(agent, conn, kind):
    session = await bound(conn)
    m = await add_m(conn, R, NOW, media=(kind, f"{kind}/x", 10, "f"))
    rig.refused(await fetch(agent, session, f"m{m}"), 422, "unsupported_type")
    assert await count(conn) == 0


async def test_fetch_foreign_media(agent, conn):
    session = await bound(conn)
    m = await add_m(conn, R, NOW, media=MEDIA)
    await conn.execute("UPDATE inbound SET media_url = 'mxc://other/abc' WHERE id = %s", (m,))
    r = await fetch(agent, session, f"m{m}")
    rig.refused(r, 422, "foreign_media")
    assert "mxc" not in r.text and "abc" not in r.text
    assert await count(conn) == 0


async def test_fetch_in_flight_dedupe(agent, conn):
    session = await bound(conn)
    m = await add_m(conn, R, NOW, media=MEDIA)
    assert (await fetch(agent, session, f"m{m}")).status_code == 202
    rig.refused(await fetch(agent, session, f"m{m}"), 409, "already_fetching")
    assert await count(conn) == 1
    await conn.execute("UPDATE attachment_requests SET status = 'fetching'")
    rig.refused(await fetch(agent, session, f"m{m}"), 409, "already_fetching")
    await conn.execute("UPDATE attachment_requests SET status = 'sent'")
    assert (await fetch(agent, session, f"m{m}")).status_code == 202


async def test_fetch_rate_limit(agent_for, conn):
    client = agent_for(agent_attachment_requests_per_10min=2)
    session = await bound(conn)
    other = await bound(conn, "!other2:x")
    ms = [await add_m(conn, R, NOW, media=MEDIA) for _ in range(3)]
    other_m = await add_m(conn, "!other2:x", NOW, media=MEDIA)
    assert (await fetch(client, session, f"m{ms[0]}")).status_code == 202
    assert (await fetch(client, session, f"m{ms[1]}")).status_code == 202
    r = await fetch(client, session, f"m{ms[2]}")
    rig.refused(r, 429, "rate_limited")
    assert r.json()["error"]["message"] == "at most 2 attachment requests per room in 600 s"
    assert (await fetch(client, other, f"m{other_m}")).status_code == 202
    assert await count(conn) == 3


async def test_status_of_other_session_is_404(agent, conn):
    session = await bound(conn)
    m = await add_m(conn, R, NOW, media=MEDIA)
    assert (await fetch(agent, session, f"m{m}")).status_code == 202
    stranger = uuid.uuid4()
    for ref in ("a1", "a0", "x", "a1/../2", "a999", "m1", "a01"):
        who = stranger if ref == "a1" else session
        r = await agent.get(f"/attachments/{ref}", headers=rig.as_(who))
        rig.refused(r, 404, "not_found")
        assert r.json()["error"]["message"] == "no such attachment request of this session"
        assert ref not in r.json()["error"]["message"]
    assert (await agent.get("/attachments/a1", headers=rig.as_(session))).status_code == 200
