import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from agents_matrix_connector.db import bindings, runs, sends

ROOM = "!a:x"


async def binding(conn, binding_id):
    cur = await conn.execute(
        "SELECT session_id, slug, released_at, release_reason FROM room_bindings WHERE id = %s",
        (binding_id,),
    )
    return await cur.fetchone()


async def count(conn):
    cur = await conn.execute("SELECT count(*) FROM room_bindings")
    return (await cur.fetchone())[0]


async def insert(conn, room, *, origin="inbound", session=None, run_id=1, send_id=None):
    cur = await conn.execute(
        "INSERT INTO room_bindings (room_id, session_id, origin, run_id, send_id)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (room, session, origin, run_id, send_id),
    )
    return (await cur.fetchone())[0]


@pytest.mark.parametrize(
    ("name", "refusal"),
    [
        ("room_bindings_active_room_idx", "room_bound"),
        ("room_bindings_active_session_idx", "session_bound"),
        ("room_bindings_pkey", None),
        (None, None),
    ],
)
def test_refusal_for_index(name, refusal):
    assert bindings.refusal_for_index(name) == refusal


async def test_one_active_binding_per_room_and_session(conn):
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    first = await insert(conn, ROOM, session=s1)
    with pytest.raises(psycopg.errors.UniqueViolation) as room_dup:
        await insert(conn, ROOM, session=s2)
    assert room_dup.value.diag.constraint_name == "room_bindings_active_room_idx"
    with pytest.raises(psycopg.errors.UniqueViolation):
        await insert(conn, ROOM, session=None, run_id=2)
    with pytest.raises(psycopg.errors.UniqueViolation) as session_dup:
        await insert(conn, "!b:x", origin="agent", session=s1, run_id=None, send_id=1)
    assert session_dup.value.diag.constraint_name == "room_bindings_active_session_idx"

    await insert(conn, "!r1:x", session=None, run_id=3)
    await insert(conn, "!r2:x", session=None, run_id=4)

    assert await bindings.release(conn, first, "session_gone") is True
    again = await insert(conn, ROOM, session=s2)
    await insert(conn, "!b:x", origin="agent", session=s1, run_id=None, send_id=1)
    assert await bindings.release(conn, again, "session_gone") is True
    await insert(conn, ROOM, session=None, run_id=5)

    with pytest.raises(psycopg.errors.CheckViolation):
        await insert(conn, "!c:x", origin="inbound", session=uuid.uuid4(), run_id=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        await insert(conn, "!c:x", origin="agent", session=None, run_id=None, send_id=1)
    with pytest.raises(psycopg.errors.CheckViolation):
        await insert(conn, "!c:x", origin="agent", session=uuid.uuid4(), run_id=None)
    await insert(conn, "!c:x", origin="agent", session=uuid.uuid4(), run_id=None, send_id=2)


async def test_reserve_fill_release(conn):
    session, other = uuid.uuid4(), uuid.uuid4()
    assert await bindings.reserve_for_run(conn, ROOM, 10) is True
    reservation = await bindings.active_for_room(conn, ROOM)
    assert (reservation.origin, reservation.run_id, reservation.session_id) == ("inbound", 10, None)
    assert await bindings.reserve_for_run(conn, ROOM, 11) is False
    assert await count(conn) == 1

    assert await bindings.fill_session(conn, 10, session, "mx-10") is True
    assert await bindings.fill_session(conn, 10, session, "mx-10") is True
    assert await binding(conn, reservation.id) == (session, "mx-10", None, None)
    assert await bindings.fill_session(conn, 10, other, "mx-other") is False
    assert await bindings.fill_session(conn, 99, session, "mx-99") is False
    assert await binding(conn, reservation.id) == (session, "mx-10", None, None)
    assert await bindings.reserve_for_run(conn, ROOM, 12) is False

    assert await bindings.release(conn, reservation.id, "session_gone", session_id=other) is False
    assert await bindings.release(conn, reservation.id, "session_gone", session_id=session) is True
    released = await binding(conn, reservation.id)
    assert released[3] == "session_gone" and released[2] is not None
    assert await bindings.release(conn, reservation.id, "again") is False
    assert await bindings.release(conn, reservation.id, "again", session_id=session) is False
    assert (await binding(conn, reservation.id))[2:] == released[2:]
    assert await bindings.fill_session(conn, 10, session, "mx-10") is False
    assert await bindings.active_for_room(conn, ROOM) is None

    assert await bindings.reserve_for_run(conn, ROOM, 13) is True
    failed = await bindings.active_for_room(conn, ROOM)
    assert await bindings.release(conn, failed.id, "run_failed") is True
    assert (await binding(conn, failed.id))[3] == "run_failed"
    assert await bindings.active_for_room(conn, ROOM) is None


async def test_bind_for_send_outcomes(conn):
    s, other, fresh = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def bind(room, session, send_id):
        return await bindings.bind_for_send(
            conn, room_id=room, session_id=session, slug="hive-1", send_id=send_id
        )

    assert await bind(ROOM, s, 1) == "bound"
    row = await bindings.active_for_room(conn, ROOM)
    assert (row.origin, row.session_id, row.slug, row.send_id, row.run_id) == (
        "agent",
        s,
        "hive-1",
        1,
        None,
    )
    assert await count(conn) == 1

    assert await bind(ROOM, s, 2) == "already"
    assert await bind(ROOM, other, 3) == "room_bound"
    assert await bind("!b:x", s, 4) == "session_bound"
    assert await bindings.reserve_for_run(conn, "!r:x", 7) is True
    assert await bind("!r:x", fresh, 5) == "room_bound"
    assert await bind("!r:x", s, 6) == "room_bound"
    assert await count(conn) == 2

    async with conn.transaction():
        assert await bind("!b:x", fresh, 8) == "bound"
        assert await bind("!b:x", fresh, 9) == "already"
    assert await count(conn) == 3
    assert (await bindings.active_for_session(conn, fresh)).room_id == "!b:x"


async def test_bind_for_send_race_maps_unique_violation(url, conn):
    """A binding committed between the checks and the insert refuses, never raises."""
    s, rival = uuid.uuid4(), uuid.uuid4()
    async with await psycopg.AsyncConnection.connect(url) as racer:
        await racer.execute(
            "INSERT INTO room_bindings (room_id, session_id, origin, send_id)"
            " VALUES (%s, %s, 'agent', 1)",
            (ROOM, rival),
        )
        async with conn.transaction():
            task = asyncio.create_task(
                bindings.bind_for_send(conn, room_id=ROOM, session_id=s, slug="x", send_id=2)
            )
            await asyncio.sleep(0.3)
            assert not task.done()
            await racer.commit()
            assert await task == "room_bound"
            cur = await conn.execute("SELECT 1")
            assert await cur.fetchone() == (1,)

        await racer.execute(
            "INSERT INTO room_bindings (room_id, session_id, origin, send_id)"
            " VALUES ('!b:x', %s, 'agent', 3)",
            (s,),
        )
        async with conn.transaction():
            task = asyncio.create_task(
                bindings.bind_for_send(conn, room_id="!c:x", session_id=s, slug="x", send_id=4)
            )
            await asyncio.sleep(0.3)
            assert not task.done()
            await racer.commit()
            assert await task == "session_bound"
    assert await count(conn) == 2


async def test_active_lookups(conn):
    s1, s2, s3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    a = await insert(conn, ROOM, session=s1, run_id=1)
    reserved = await insert(conn, "!r:x", session=None, run_id=2)
    b = await insert(conn, "!b:x", origin="agent", session=s2, run_id=None, send_id=5)
    gone = await insert(conn, "!c:x", session=s3, run_id=3)
    await bindings.release(conn, gone, "session_gone")

    room_a = await bindings.active_for_room(conn, ROOM)
    assert room_a == bindings.BindingRow(
        id=a,
        room_id=ROOM,
        session_id=s1,
        slug=None,
        origin="inbound",
        run_id=1,
        send_id=None,
        created_at=room_a.created_at,
        released_at=None,
        release_reason=None,
    )
    assert await bindings.active_for_session(conn, s1) == room_a
    assert (await bindings.active_for_room(conn, "!r:x")).id == reserved
    assert (await bindings.active_for_session(conn, s2)).id == b
    assert await bindings.active_for_room(conn, "!c:x") is None
    assert await bindings.active_for_session(conn, s3) is None
    assert [r.id for r in await bindings.active_with_session(conn)] == [a, b]

    # Runs and sends with a session never count as a binding.
    lonely, run_session = uuid.uuid4(), uuid.uuid4()
    ref = await runs.create_run(conn, room_id="!d:x", preset_id=str(uuid.uuid4()), kind="first")
    await conn.execute("UPDATE runs SET session_id = %s WHERE id = %s", (run_session, ref.id))
    await sends.insert(
        conn,
        session_id=lonely,
        slug="hive-2",
        kind="message",
        room_id="!e:x",
        text="hi",
        key=None,
        reply_to=None,
        ref_event_id=None,
    )
    assert await bindings.active_for_room(conn, "!d:x") is None
    assert await bindings.active_for_room(conn, "!e:x") is None
    assert await bindings.active_for_session(conn, run_session) is None
    assert await bindings.active_for_session(conn, lonely) is None
    assert [r.id for r in await bindings.active_with_session(conn)] == [a, b]


async def test_prune_released_bindings(conn):
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=1)
    old = now - timedelta(days=3)
    active_old = await insert(conn, ROOM, session=uuid.uuid4(), run_id=1)
    released_old = await insert(conn, "!b:x", session=uuid.uuid4(), run_id=2)
    released_new = await insert(conn, "!c:x", session=uuid.uuid4(), run_id=3)
    reservation_old = await insert(conn, "!d:x", session=None, run_id=4)
    for released in (released_old, released_new, reservation_old):
        await bindings.release(conn, released, "session_gone")
    await conn.execute(
        "UPDATE room_bindings SET created_at = %s WHERE id = ANY(%s)",
        (old, [active_old, released_old, released_new, reservation_old]),
    )
    await conn.execute(
        "UPDATE room_bindings SET released_at = %s WHERE id = ANY(%s)",
        (old, [released_old, reservation_old]),
    )

    assert await bindings.prune_released(conn, cutoff) == 2
    cur = await conn.execute("SELECT id FROM room_bindings ORDER BY id")
    assert [r[0] for r in await cur.fetchall()] == [active_old, released_new]
    assert await bindings.prune_released(conn, cutoff) == 0
