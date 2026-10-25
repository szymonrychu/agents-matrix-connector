import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from agents_common.db.pool import open_pool
from agents_common.runs import RunRef, RunStore

from agents_matrix_connector.db import runs

PACKAGE = "agents_matrix_connector"
ROOM = "!a:x"


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


async def new_run(conn, room=ROOM, kind="first") -> RunRef:
    return await runs.create_run(conn, room_id=room, preset_id=str(uuid.uuid4()), kind=kind)


async def test_run_store_protocol_and_guards(conn, pool):
    store = runs.PgRunStore(pool)
    assert isinstance(store, RunStore)
    ref = await new_run(conn)
    sid = str(uuid.uuid4())
    await store.set_session(ref.id, sid)
    await store.set_message(ref.id, "42")
    row = (await runs.in_progress(conn))[0]
    assert (row.session_id, row.message_id) == (uuid.UUID(sid), "42")

    assert await store.finish(ref.id, "failed", "boom") is True
    assert await store.finish(ref.id, "handled", None) is False
    await store.set_session(ref.id, str(uuid.uuid4()))
    await store.set_message(ref.id, "99")
    cur = await conn.execute(
        "SELECT status, reason, session_id, message_id, finished_at FROM runs WHERE id = %s",
        (ref.id,),
    )
    status, reason, session_id, message_id, finished_at = await cur.fetchone()
    assert (status, reason, session_id, message_id) == ("failed", "boom", uuid.UUID(sid), "42")
    assert finished_at is not None
    assert await runs.finish(conn, ref.id, "handled") is False
    with pytest.raises(ValueError):
        await runs.finish(conn, ref.id, "in-progress")


async def test_create_run_and_latest_session(conn):
    assert await runs.latest_session(conn, ROOM) is None
    first = await new_run(conn)
    assert first.slug == f"mx-{first.id}"
    assert first.session_id is None and first.message_id is None
    assert isinstance(first.id, int)
    assert await runs.latest_session(conn, ROOM) is None

    s1, s2 = uuid.uuid4(), uuid.uuid4()
    await conn.execute("UPDATE runs SET session_id = %s WHERE id = %s", (s1, first.id))
    second = await new_run(conn, kind="follow")
    await conn.execute("UPDATE runs SET session_id = %s WHERE id = %s", (s2, second.id))
    await new_run(conn, kind="follow")
    other = await new_run(conn, room="!b:x")
    await conn.execute("UPDATE runs SET session_id = %s WHERE id = %s", (uuid.uuid4(), other.id))
    assert await runs.latest_session(conn, ROOM) == (second.id, s2)
    with pytest.raises(ValueError):
        await runs.create_run(conn, room_id=ROOM, preset_id=str(uuid.uuid4()), kind="other")


async def test_count_first_in_progress(conn):
    a = await new_run(conn, room="!a:x")
    await new_run(conn, room="!b:x")
    await new_run(conn, room="!c:x", kind="follow")
    assert await runs.count_first_in_progress(conn) == 2
    assert len(await runs.in_progress(conn)) == 3
    await runs.finish(conn, a.id, "handled")
    assert await runs.count_first_in_progress(conn) == 1
    assert len(await runs.in_progress(conn)) == 2


async def test_outbound_txn_ids_and_guards(conn, pool):
    ref = await new_run(conn)
    ids = await runs.add_outbound(
        conn, room_id=ROOM, run_id=ref.id, kind="reply", chunks=["a", "b"]
    )
    assert len(ids) == 2
    again = await runs.add_outbound(
        conn, room_id=ROOM, run_id=ref.id, kind="reply", chunks=["a", "b"]
    )
    assert again == ids
    notice = await runs.add_outbound(
        conn, room_id=ROOM, run_id=ref.id, kind="notice", chunks=["sorry"]
    )
    assert (
        await runs.add_outbound(conn, room_id=ROOM, run_id=ref.id, kind="notice", chunks=["sorry"])
        == notice
    )
    with pytest.raises(ValueError):
        await runs.add_outbound(conn, room_id=ROOM, run_id=ref.id, kind="notice", chunks=["a", "b"])

    pending = await runs.pending(conn)
    assert [p.id for p in pending] == [*ids, *notice]
    assert [p.txn_id for p in pending] == [
        f"mxc-{ref.id}-1",
        f"mxc-{ref.id}-2",
        f"mxc-{ref.id}-notice",
    ]
    assert [p.body for p in pending] == ["a", "b", "sorry"]

    assert await runs.mark_sent(conn, ids[0], "$e1") is True
    assert await runs.mark_sent(conn, ids[0], "$e2") is False
    assert await runs.mark_failed(conn, ids[0], "late") is False
    assert await runs.mark_failed(conn, ids[1], "rejected") is True
    assert await runs.mark_sent(conn, ids[1], "$e3") is False
    assert [p.id for p in await runs.pending(conn)] == notice

    listed = await runs.list_outbound(pool, ROOM, limit=10)
    assert [r.id for r in listed] == [*notice, ids[1], ids[0]]
    sent = listed[2]
    assert (sent.status, sent.event_id, sent.sent_at is not None) == ("sent", "$e1", True)
    assert listed[1].reason == "rejected"
    older = await runs.list_outbound(pool, ROOM, limit=1, before_id=notice[0])
    assert [r.id for r in older] == [ids[1]]
    assert await runs.list_outbound(pool, "!zzz:x", limit=10) == []


async def test_prune(conn):
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=1)
    old = now - timedelta(days=3)
    done_old = await new_run(conn, room="!1:x")
    done_new = await new_run(conn, room="!2:x")
    open_old = await new_run(conn, room="!3:x")
    failed_old = await new_run(conn, room="!4:x")
    for r in (done_old, done_new, failed_old):
        await runs.finish(conn, r.id, "handled" if r is not failed_old else "failed", "x")
    await conn.execute(
        "UPDATE runs SET finished_at = %s WHERE id = ANY(%s)", (old, [done_old.id, failed_old.id])
    )
    await conn.execute("UPDATE runs SET started_at = %s WHERE id = %s", (old, open_old.id))

    o_sent, o_failed, o_pending, o_new = await runs.add_outbound(
        conn, room_id=ROOM, run_id=done_new.id, kind="reply", chunks=["sent", "failed", "p", "n"]
    )
    await runs.mark_sent(conn, o_sent, "$e")
    await runs.mark_failed(conn, o_failed, "x")
    await runs.mark_sent(conn, o_new, "$n")
    await conn.execute(
        "UPDATE outbound SET created_at = %s WHERE id = ANY(%s)",
        (old, [o_sent, o_failed, o_pending]),
    )

    assert await runs.prune(conn, cutoff) == 4
    cur = await conn.execute("SELECT id FROM runs ORDER BY id")
    assert [r[0] for r in await cur.fetchall()] == [done_new.id, open_old.id]
    cur = await conn.execute("SELECT id FROM outbound ORDER BY id")
    assert [r[0] for r in await cur.fetchall()] == [o_pending, o_new]
