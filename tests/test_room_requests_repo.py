import uuid

import pytest

from agents_matrix_connector.db import room_requests as rr


async def put(conn, *, action="open", status="pending_confirmation", room_id=None, **kw):
    return await rr.insert(
        conn,
        action=action,
        status=status,
        target_kind=kw.pop("target_kind", "facebook"),
        target=kw.pop("target", "123"),
        room_id=room_id,
        task_key=kw.pop("task_key", "T-25"),
        **kw,
    )


async def status_of(conn, request_id):
    return (await rr.get(conn, request_id)).status


async def test_insert_and_get(conn):
    session = uuid.uuid4()
    row = await rr.insert(
        conn,
        action="open",
        status="candidates",
        target_kind="name",
        target="Jane",
        candidates=[{"user_id": "@a:x", "display_name": "Jane", "facebook_id": None}],
        task_key="T-1",
        requested_by=session,
        requested_slug="hive-1",
    )
    got = await rr.get(conn, row.id)
    assert got == row
    assert got.action == "open"
    assert got.status == "candidates"
    assert got.candidates == [{"user_id": "@a:x", "display_name": "Jane", "facebook_id": None}]
    assert got.requested_by == session
    assert got.requested_slug == "hive-1"
    assert got.decision_id is None and got.confirmed_at is None and got.done_at is None
    assert got.created_at is not None and got.updated_at is not None
    assert await rr.get(conn, row.id + 1000) is None
    with pytest.raises(ValueError):
        await rr.insert(conn, action="close", status="resolving")
    with pytest.raises(ValueError):
        await rr.insert(conn, action="open", status="weird")


async def test_attach_decision_only_once(conn):
    row = await put(conn)
    assert await rr.attach_decision(conn, row.id, "T-25", 7) is True
    got = await rr.get(conn, row.id)
    assert (got.decision_id, got.task_key, got.status) == (7, "T-25", "pending_confirmation")
    assert await rr.attach_decision(conn, row.id, "T-25", 8) is False
    assert (await rr.get(conn, row.id)).decision_id == 7
    other = await put(conn, status="resolving")
    assert await rr.attach_decision(conn, other.id, "T-25", 9) is False


async def test_confirm_needs_matching_decision(conn):
    row = await put(conn)
    assert await rr.confirm(conn, row.id, 7, True) is False
    await rr.attach_decision(conn, row.id, "T-25", 7)
    assert await rr.confirm(conn, row.id, 8, True) is False
    assert await status_of(conn, row.id) == "pending_confirmation"
    assert await rr.confirm(conn, row.id, 7, True) is True
    got = await rr.get(conn, row.id)
    assert got.status == "confirmed" and got.confirmed_at is not None
    assert await rr.confirm(conn, row.id, 7, False) is False
    assert await status_of(conn, row.id) == "confirmed"


async def test_confirm_rejected_sets_reason(conn):
    row = await put(conn)
    await rr.attach_decision(conn, row.id, "T-25", 3)
    assert await rr.confirm(conn, row.id, 3, False) is True
    got = await rr.get(conn, row.id)
    assert (got.status, got.reason) == ("rejected", "user_declined")


async def test_claim_confirmed_moves_to_working(conn):
    a = await put(conn, status="confirmed")
    b = await put(conn, action="accept", status="confirmed", room_id="!r:x", target_kind=None)
    pending = await put(conn)
    claimed = await rr.claim_confirmed(conn)
    assert [r.id for r in claimed] == [a.id, b.id]
    assert {r.status for r in claimed} == {"working"}
    assert await rr.claim_confirmed(conn) == []
    assert await status_of(conn, pending.id) == "pending_confirmation"
    assert await rr.release(conn, a.id) is True
    assert await status_of(conn, a.id) == "confirmed"
    assert await rr.release(conn, a.id) is False


async def test_done_guarded(conn):
    row = await put(conn, status="confirmed")
    assert await rr.done(conn, row.id, "!r:x") is False
    await rr.claim_confirmed(conn)
    assert await rr.done(conn, row.id, "!r:x", "join_pending") is True
    got = await rr.get(conn, row.id)
    assert (got.status, got.room_id, got.reason) == ("done", "!r:x", "join_pending")
    assert got.done_at is not None
    assert await rr.done(conn, row.id, "!r:x") is False


async def test_resolving_transitions(conn):
    a = await put(conn, status="resolving", target_kind="name", target="Jane")
    b = await put(conn, status="resolving", target_kind="name", target="Bob")
    c = await put(conn, status="resolving", target_kind="name", target="Nobody")
    assert [r.id for r in await rr.claim_resolving(conn)] == [a.id, b.id, c.id]
    assert await rr.resolved(conn, a.id, target_kind="facebook", target="9", display_name="J")
    got = await rr.get(conn, a.id)
    assert (got.status, got.target_kind, got.target, got.display_name) == (
        "pending_confirmation",
        "facebook",
        "9",
        "J",
    )
    cands = [{"user_id": None, "display_name": "Bob", "facebook_id": "1"}]
    assert await rr.to_candidates(conn, b.id, cands) is True
    assert (await rr.get(conn, b.id)).candidates == cands
    assert await rr.fail(conn, c.id, "resolving", "no_match") is True
    got = await rr.get(conn, c.id)
    assert (got.status, got.reason) == ("failed", "no_match")
    assert (
        await rr.resolved(conn, a.id, target_kind="facebook", target="9", display_name="J") is False
    )
    assert await rr.fail(conn, a.id, "resolving", "x") is False
    assert await rr.claim_resolving(conn) == []


async def test_status_change_bumps_updated_at(conn):
    row = await put(conn)
    await conn.execute(
        "UPDATE room_requests SET updated_at = now() - interval '1 hour' WHERE id = %s", (row.id,)
    )
    before = (await rr.get(conn, row.id)).updated_at
    await rr.attach_decision(conn, row.id, "T-25", 1)
    assert (await rr.get(conn, row.id)).updated_at > before


async def test_count_recent_opens_ignores_accept(conn):
    await put(conn)
    await put(conn, action="invite", target_kind="matrix", target="@a:x", room_id="!r:x")
    await put(conn, action="accept", status="confirmed", room_id="!r:x", target_kind=None)
    old = await put(conn)
    await conn.execute(
        "UPDATE room_requests SET created_at = now() - interval '2 hours' WHERE id = %s", (old.id,)
    )
    assert await rr.count_recent_opens(conn, 3600) == 2
    assert await rr.count_recent_opens(conn, 3 * 3600) == 3


async def test_joinable_room_ids(conn):
    working = await put(conn, status="confirmed")
    await rr.claim_confirmed(conn)
    await conn.execute("UPDATE room_requests SET room_id = '!w:x' WHERE id = %s", (working.id,))
    done = await put(conn, status="confirmed")
    await rr.claim_confirmed(conn)
    await rr.done(conn, done.id, "!d:x")
    invite = await put(
        conn, action="invite", status="confirmed", target_kind="matrix", room_id="!i:x"
    )
    await rr.claim_confirmed(conn)
    await rr.done(conn, invite.id, "!i:x")
    await put(conn, status="confirmed", room_id="!c:x")
    assert await rr.joinable_room_ids(conn) == {"!w:x", "!d:x"}


async def test_has_open_accept(conn):
    assert await rr.has_open_accept(conn, "!r:x") is False
    row = await put(conn, action="accept", status="confirmed", room_id="!r:x", target_kind=None)
    assert await rr.has_open_accept(conn, "!r:x") is True
    await rr.claim_confirmed(conn)
    assert await rr.has_open_accept(conn, "!r:x") is True
    await rr.done(conn, row.id, "!r:x")
    assert await rr.has_open_accept(conn, "!r:x") is False
