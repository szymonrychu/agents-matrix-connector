from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from agents_common.db.pool import open_pool

from agents_matrix_connector.db import inbound

PACKAGE = "agents_matrix_connector"
T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


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


async def put(conn, event_id, room="!a:x", status="new", mentioned=False, reason=None):
    return await inbound.insert(
        conn,
        event_id=event_id,
        room_id=room,
        sender="@u:x",
        sender_name="U",
        body=f"body {event_id}",
        mentioned=mentioned,
        origin_ts=T0,
        status=status,
        reason=reason,
    )


async def new_run(conn, room="!a:x", status="in-progress") -> int:
    cur = await conn.execute(
        "INSERT INTO runs (room_id, preset_id, kind, status)"
        " VALUES (%s, gen_random_uuid(), 'first', %s) RETURNING id",
        (room, status),
    )
    return (await cur.fetchone())[0]


async def statuses(conn) -> dict[str, str]:
    cur = await conn.execute("SELECT event_id, status FROM inbound")
    return dict(await cur.fetchall())


async def test_insert_idempotent(conn):
    assert await put(conn, "$1", mentioned=True) is True
    assert await put(conn, "$1", status="ignored", reason="dup") is False
    cur = await conn.execute("SELECT count(*), min(status), bool_and(mentioned) FROM inbound")
    assert await cur.fetchone() == (1, "new", True)
    assert await inbound.count_new(conn, "!a:x") == 1
    assert await put(conn, "$2", status="ignored", reason="denied") is True
    assert await inbound.count_new(conn, "!a:x") == 1


async def test_rooms_ready(conn):
    await put(conn, "$b1", room="!b:x")
    await put(conn, "$a1", room="!a:x")
    await put(conn, "$c1", room="!c:x")
    await put(conn, "$b2", room="!b:x")
    await put(conn, "$d1", room="!d:x", status="ignored", reason="x")
    assert await inbound.rooms_ready(conn) == ["!b:x", "!a:x", "!c:x"]
    await new_run(conn, "!a:x")
    await new_run(conn, "!c:x", status="handled")
    assert await inbound.rooms_ready(conn) == ["!b:x", "!c:x"]


async def test_claim_room(conn):
    await put(conn, "$1")
    await put(conn, "$o", room="!o:x")
    await put(conn, "$2")
    await put(conn, "$ig", status="ignored", reason="x")
    run = await new_run(conn)
    rows = await inbound.claim_room(conn, "!a:x", run)
    assert [r.event_id for r in rows] == ["$1", "$2"]
    assert all(r.status == "in-progress" and r.run_id == run for r in rows)
    assert rows[0].body == "body $1" and rows[0].origin_ts == T0
    assert await statuses(conn) == {
        "$1": "in-progress",
        "$2": "in-progress",
        "$o": "new",
        "$ig": "ignored",
    }
    assert await inbound.claim_room(conn, "!a:x", run) == []


async def test_run_rows(conn):
    await put(conn, "$1")
    await put(conn, "$o", room="!o:x")
    await put(conn, "$2")
    run, other = await new_run(conn), await new_run(conn, "!o:x")
    await inbound.claim_room(conn, "!a:x", run)
    await inbound.claim_room(conn, "!o:x", other)
    await put(conn, "$3")
    rows = await inbound.run_rows(conn, run)
    assert [r.event_id for r in rows] == ["$1", "$2"]
    assert all(r.status == "in-progress" and r.run_id == run for r in rows)
    assert rows[0].body == "body $1" and rows[0].origin_ts == T0
    await inbound.finish_run_rows(conn, other, "handled")
    assert await inbound.run_rows(conn, other) == []


async def test_guarded_transitions(conn):
    await put(conn, "$1")
    await put(conn, "$2")
    await put(conn, "$o", room="!o:x")
    run, other = await new_run(conn), await new_run(conn, "!o:x")
    await inbound.claim_room(conn, "!a:x", run)
    await inbound.claim_room(conn, "!o:x", other)
    assert await inbound.ignore_new(conn, "!a:x", "paused") == 0
    assert await inbound.finish_run_rows(conn, run + 100, "handled") == 0
    assert await inbound.requeue_run_rows(conn, run) == 2
    cur = await conn.execute("SELECT count(*) FROM inbound WHERE run_id IS NOT NULL")
    assert (await cur.fetchone())[0] == 1
    assert await inbound.finish_run_rows(conn, run, "handled") == 0
    assert await inbound.ignore_new(conn, "!a:x", "paused") == 2
    assert await inbound.ignore_new(conn, "!a:x", "paused") == 0
    assert await inbound.finish_run_rows(conn, other, "failed", "boom") == 1
    assert await inbound.finish_run_rows(conn, other, "handled") == 0
    assert await inbound.requeue_run_rows(conn, other) == 0
    cur = await conn.execute("SELECT status, reason FROM inbound WHERE event_id = '$o'")
    assert await cur.fetchone() == ("failed", "boom")
    cur = await conn.execute("SELECT DISTINCT reason FROM inbound WHERE room_id = '!a:x'")
    assert await cur.fetchall() == [("paused",)]
    with pytest.raises(ValueError):
        await inbound.finish_run_rows(conn, run, "new")


async def test_requeue_keeps_fifo(conn):
    await put(conn, "$1")
    run = await new_run(conn)
    await inbound.claim_room(conn, "!a:x", run)
    await put(conn, "$2")
    await inbound.requeue_run_rows(conn, run)
    rows = await inbound.claim_room(conn, "!a:x", run)
    assert [r.event_id for r in rows] == ["$1", "$2"]


async def test_list_and_prune(conn, pool):
    for i in range(1, 6):
        await put(conn, f"$a{i}")
    await put(conn, "$b1", room="!b:x", status="ignored", reason="x")
    run = await new_run(conn)
    await inbound.claim_room(conn, "!a:x", run)
    await inbound.finish_run_rows(conn, run, "handled")
    allrows = await inbound.list_messages(pool)
    assert [r.event_id for r in allrows] == ["$b1", "$a5", "$a4", "$a3", "$a2", "$a1"]
    assert [r.event_id for r in await inbound.list_messages(pool, room_id="!b:x")] == ["$b1"]
    assert [r.event_id for r in await inbound.list_messages(pool, status="ignored")] == ["$b1"]
    page = await inbound.list_messages(pool, room_id="!a:x", limit=2, before_id=allrows[1].id)
    assert [r.event_id for r in page] == ["$a4", "$a3"]

    await put(conn, "$new")
    await put(conn, "$busy", room="!c:x")
    await inbound.claim_room(conn, "!c:x", await new_run(conn, "!c:x"))
    await conn.execute("UPDATE inbound SET received_at = %s", (T0 - timedelta(days=10),))
    await conn.execute(
        "UPDATE inbound SET received_at = %s WHERE event_id = '$a5'", (T0 + timedelta(days=1),)
    )
    assert await inbound.prune(conn, T0) == 5
    left = await statuses(conn)
    assert left == {"$a5": "handled", "$new": "new", "$busy": "in-progress"}
